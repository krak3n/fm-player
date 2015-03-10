#!/usr/bin/env python
# encoding: utf-8

"""
fmplayer.player
===============

Classes and methods for running the Spotify player.
"""

import gevent
import json
import logging
import spotify
import threading
import random

from fmplayer.sinks import FakeSink


logger = logging.getLogger('fmplayer')

LOGGED_IN_EVENT = threading.Event()
STOP_EVENT = threading.Event()

PLAYLIST_KEY = 'fm:player:queue'


class Player(object):
    """ Handles playing music from Spotify.
    """

    def __init__(self, user, password, key, sink):
        """ Initialises the Spotify Session, logs the user in and starts
        the session event loop. The player does not manage state, it simply
        cares about playing music.

        Arguments
        ---------
        user : str
            The Spotify User
        password : str
            The Spotify User Password
        key : str
            Path to the Spotify API Key File
        sink : str
            The audio sink to use
        """

        # Session Configuration
        logger.debug('Configuring Spotify Session')
        config = spotify.Config()
        config.load_application_key_file(key)
        config.dont_save_metadata_for_playlists = True
        config.initially_unload_playlists = True

        # Create session
        logger.debug('Creating Session')
        self.session = spotify.Session(config)
        self.register_session_events()

        # Set the session event loop going
        logger.debug('Starting Spotify Event Loop')
        loop = spotify.EventLoop(self.session)
        loop.start()

        # Block until Login is complete
        logger.debug('Waiting for Login to Complete...')
        self.session.login(user, password)
        LOGGED_IN_EVENT.wait()

        # Set the Audio Sink for the Session
        sinks = {
            'alsa': spotify.AlsaSink,
            'fake': FakeSink
        }
        logger.info('Settingw Audio Sink to: {0}'.format(sink))
        sinks.get(sink, FakeSink)(self.session)

    def register_session_events(self):
        """ Sets up session events to listen for and set an appropriate
        callback function.
        """

        self.session.on(
            spotify.SessionEvent.CONNECTION_STATE_UPDATED,
            self.on_connection_state_updated)

        self.session.on(
            spotify.SessionEvent.END_OF_TRACK,
            self.on_track_of_end)

    def on_connection_state_updated(self, session):
        """ Fired when the connect to Spotify changes
        """

        if session.connection.state is spotify.ConnectionState.LOGGED_IN:
            logger.info('Login Complete')
            LOGGED_IN_EVENT.set()  # Unblocks the player from starting

    def on_track_of_end(self, session):
        """ Fired when a playing track finishes, ensures the tack is unloaded
        and the ``STOP_EVENT`` is set to ``True``.
        """

        logger.debug('Track End - Unloading')
        session.player.unload()
        logger.debug('STOP_EVENT set')
        STOP_EVENT.set()  # Unblocks the playlist watcher

    def play(self, uri):
        """ Plays a given Spotify URI. Ensures the ``STOP_EVENT`` event is set
        back to ``False``, loads and then plays the track.

        Arguments
        ---------
        uri : str
            The Spotify URI - e.g: ``spotify:track:3Esqxo3D31RCjmdgwBPbOO``
        """

        logger.info('Play Track: {0}'.format(uri))

        try:
            track = self.session.get_track(uri).load()
            self.session.player.load(track)
            self.session.player.play()
        except (spotify.error.LibError, ValueError):
            logger.error('Unable to play: {0}'.uri)
        else:
            logger.debug('STOP_EVENT cleared')
            STOP_EVENT.clear()  # Reset STOP_EVENT flag to False

    def pause(self):
        """ Pauses the current playback if the track is in a playing state.
        """

        if self.session.player.state == spotify.PlayerState.PLAYING:
            logger.info('Pausing Playback')
            self.session.player.pause()
        else:
            logger.debug('Cannot Pause - No Track Playing')

    def resume(self):
        """ Resumes playback if the player is in a paused state.
        """

        if self.session.player.state == spotify.PlayerState.PAUSED:
            logger.info('Resuming Playback')
            self.session.player.play()
        else:
            logger.debug('Cannot Resume - Not in paused state')


def queue_watcher(redis, player, channel):
    """ This method watches the playlist queue for tracks, once the queue has
    a track the player will be told to play the track, this will cause the
    method to block until the track has completed playing the track. Once the
    track is finished we will go round again.

    Arguments
    ---------
    redis : obj
        Redis connection instance
    player : obj
        Player instance
    channel : str
        Redis PubSub channel
    """

    logger.info('Watching Playlist')

    while True:
        if redis.llen(PLAYLIST_KEY) > 0:
            uri = redis.lpop(PLAYLIST_KEY)
            logger.debug('Track popped of list: {0}'.format(uri))

            # Play the track
            player.play(uri)

            logger.debug('Publish Play Event'.format(uri))
            redis.publish(channel, json.dumps({
                'event': 'play',
                'uri': uri
            }))
            logger.debug('Waiting for {0} to Finish'.format(uri))

            # Block until the player stops playing
            STOP_EVENT.wait()

            logger.debug('Publish Stop Event'.format(uri))
            redis.publish(channel, json.dumps({
                'event': 'end',
                'uri': uri
            }))

        gevent.sleep(random.randint(0, 2) * 0.001)


def event_watcher(redis, player, channel):
    """ This method watches the Redis PubSub channel for events. Once a valid
    event is fired it will execute the desired functionality for that event.

    Arguments
    ---------
    redis : obj
        Redis connection instance
    player : obj
        Player instance
    channel : str
        Redis PubSub channel
    """

    logger.info('Starting Redis Event Loop')

    pubsub = redis.pubsub()
    pubsub.subscribe(channel)

    events = {
        'pause': player.pause,
        'resume': player.resume,
    }

    for item in pubsub.listen():
        logger.debug('Got Event: {0}'.format(item))
        if item.get('type') == 'message':
            data = json.loads(item.get('data'))
            event = data.get('event')
            if event in events:
                logger.debug('Fire: {0}'.format(event))
                function = events.get(event)
                function()
