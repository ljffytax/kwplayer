
import copy
from gi.repository import Gdk
from gi.repository import GdkPixbuf
from gi.repository import GObject
import json
import os
import sqlite3
import threading
import time
from urllib import parse
from urllib import request

from kuwo import Config
from kuwo import Utils

SEARCH = 'http://search.kuwo.cn/r.s?'
QUKU = 'http://qukudata.kuwo.cn/q.k?'
TOPLIST = 'http://kbangserver.kuwo.cn/ksong.s?'
LRC = 'http://newlyric.kuwo.cn/newlyric.lrc?'
ARTIST_LOGO = 'http://img4.kwcdn.kuwo.cn/star/starheads/'
SONG = 'http://antiserver.kuwo.cn/anti.s?'
CHUNK = 16 * 1024
CHUNK_TO_PLAY = 1024 * 1024

conf = Config.load_conf()
conn = sqlite3.connect(conf['cache-db'])
cursor = conn.cursor()
def close():
    conn.commit()
    conn.close()
    print('db closed')

# calls f on another thread
def async_call(func, func_done, *args):
    def do_call(*args):
        result = None
        error = None

        try:
            result = func(*args)
        except Exception as e:
            error = e

        GObject.idle_add(lambda: func_done(result, error))

    thread = threading.Thread(target=do_call, args=args)
    thread.start()

def get_image(url):
    '''
    Return local image path if exists,
    or retrieve from url and save it to filepath.
    If both fails, return None
    '''
    def _parse_image(url): 
        print('url-image:', url)
        req = request.urlopen(url)
        if req.status != 200:
            return None
        return req.read()
    
    def _dump_image(image, filepath):
        with open(filepath, 'wb') as fh:
            fh.write(image)

    filename = os.path.split(url)[1]
    filepath = os.path.join(conf['img-dir'], filename)
    image = _parse_image(url)
    if image is not None:
        _dump_image(image, filepath)
        return filepath
    return None

def update_liststore_image(liststore, path, col, url):
    '''
    Update images in IconView(liststore).
    '''
    def _update_image(filepath, error):
        if filepath is None:
            return
        #Gdk.threads_enter()
        pix = GdkPixbuf.Pixbuf.new_from_file(filepath)
        liststore[path][col] = pix
        #Gdk.threads_leave()
    
    # image image is cached locally, just load them.
    filename = os.path.split(url)[1]
    filepath = os.path.join(conf['img-dir'], filename)
    if os.path.exists(filepath):
        _update_image(filepath, None)
        return

    print('update_liststore_image:', url)
    async_call(get_image, _update_image, url)

def get_lrc(rid):
    '''
    Get lrc content of specific song with UTF-8
    rid like this: '928003'
    '''
    def _parse_lrc():
        url = LRC + Utils.encode_lrc_url(rid)
        print('lrc url:', url)
        req = request.urlopen(url)
        if req.status != 200:
            return None
        data = req.read()
        try:
            lrc = Utils.decode_lrc_content(data)
        except Exception as e:
            print(e)
            return None
        return lrc

    filepath = os.path.join(conf['lrc-dir'], rid + '.lrc')
    if os.path.exists(filepath):
        with open(filepath) as fh:
            return fh.read()

    lrc = _parse_lrc()
    if lrc is not None:
        with open(filepath, 'w') as fh:
            fh.write(lrc)
        return lrc
    return None

def search(keyword, _type, page=0):
    '''
    Search songs, albums, MV.
    No local cache.
    '''
    url = ''
    if _type == 'all':
        url = ''.join([
            SEARCH,
            'all=',
            parse.quote(keyword),
            '&rformat=json&encoding=UTF8&rn=50',
            '&pn=',
            str(page),
            ])
    print('url-search:', url)
    req = request.urlopen(url)
    if req.status != 200:
        return None
    txt = req.read().decode('gbk').replace("'", '"')
    return json.loads(txt)


class ArtistSong:
    '''
    artist operations like get songs
    Create this class because we need to store some private information to
    simplify the design of program.
    '''
    def __init__(self, artist):
        self.artist = artist
        self.page = 0
        self.total_songs = 0

        self.init_tables()

    def init_tables(self):
        sql = '''
        CREATE TABLE IF NOT EXISTS `artistmusic` (
        id INTEGER PRIMARY KYE AUTOINCREMENT,
        artist CHAR,
        pn INT,
        songs TEXT
        )
        '''
        cursor.execute(sql)
        conn.commit()

    def get_songs(self):
        #Rearch the max num
        if self.total_songs > 0 and self.page * 50 > self.total_songs:
            return None

        songs = self._read_songs()
        if songs is None:
            songs = self._parse_songs()
            if songs is not None:
                self._dump_songs(songs)
        self.page += 1
        if self.total_songs == 0:
            self.total_songs = int(songs['TOTAL'])
        return songs['abslist']

    def _read_songs(self):
        sql = 'SELECT songs FROM `artistmusic` WHERE artist=? AND pn=? LIMIT 1'
        req = cursor.execute(sql, (self.artist, self.page))
        songs = req.fetchone()
        if songs is not None:
            print('local song cache HIT!')
            return json.loads(songs[0])
        return None

    def _parse_songs(self):
        '''
        Get 50 songs of this artist.
        '''
        url = ''.join([
            SEARCH,
            'ft=music&rn=50&itemset=newkw&newsearch=1&cluster=0',
            '&primitive=0&rformat=json&encoding=UTF8&artist=',
            parse.quote(self.artist, encoding='GBK'),
            '&pn=',
            str(self.page),
            ])
        print('url-songs:', url)
        req = request.urlopen(url)
        if req.status != 200:
            return None
        try:
            songs = json.loads(req.read().decode().replace("'", '"'))
        except Error as e:
            print(e)
            return None
        return songs

    def _dump_songs(self, songs):
        sql = 'INSERT INTO `artistmusic` VALUES(?, ?, ?, ?)'
        cursor.execute(sql, (self.artist, self.page, 
            json.dumps(songs), int(time.time())))
        conn.commit()


def get_artist_info(callback, artist=None, artistid=None):
    '''
    Get artist info, if cached, just return it.
    At least one of these parameters is specified, and artistid is prefered.

    This function uses async_call(), and the callback function is called 
    when the artist info is retrieved.

    Artist logo is also retrieved and saved to info['logo']
    '''
    def _init_table():
        sql = '''
        CREATE TABLE IF NOT EXISTS `artistinfo` (
        artist CHAR,
        artistid INTEGER,
        info TEXT
        )
        '''
        cursor.execute(sql)
        conn.commit()

    def _read_info():
        if artist is not None:
            sql = 'SELECT info FROM `artistinfo` WHERE artist=? LIMIT 1'
            req = cursor.execute(sql, (artist, ))
        else:
            sql = 'SELECT info FROM `artistinfo` WHERE artistid=? LIMIT 1'
            req = cursor.execute(sql, (artistid, ))
        info = req.fetchone()
        if info is not None:
            return json.loads(info[0])
        return None

    def _write_info(info):
        sql = 'INSERT INTO `artistinfo`(artist, artistid, info) VALUES(?, ?, ?)'
        cursor.execute(sql, (info['name'], int(info['id']), json.dumps(info)))
        conn.commit()

    def _parse_info():
        if artist is not None:
            url = ''.join([SEARCH, 'stype=artistinfo&artist=',
                parse.quote(artist), ])
        else:
            url = ''.join([SEARCH, 'stype=artistinfo&artistid=', artistid])
        print('artist-info:', url)
        req = request.urlopen(url)
        if req.status != 200:
            return None
        try:
            info = json.loads(req.read().decode().replace("'", '"'))
        except Error as e:
            print(e)
            return None

        # set logo size to 120x120
        logo_id = info['pic']
        if logo_id[:3] in ('55/', '90/', '100'):
            logo_id = '120/' + logo_id[3:]
        url = ARTIST_LOGO + logo_id
        info['logo'] = get_image(url)
        return info

    def _update_info(info, error):
        if info is None:
            return
        _write_info(info)
        callback(info, error)

    if artist is None and artistid is None:
        print('Error, at least one of atist and artistid is needed')
        return None
    _init_table()

    info = _read_info()
    if info is not None:
        callback(info, None)

    async_call(_parse_info, _update_info)


class Node:
    '''
    Get content of nodes from nid=2 to nid=15
    '''
    def __init__(self, nid):
        self.nid = nid

        self.init_tables()

    def init_tables(self):
        sql = '''
        CREATE TABLE IF NOT EXISTS `nodes` (
        nid INT,
        info TEXT,
        timestamp INT
        )
        '''
        cursor.execute(sql)
        conn.commit()

    def get_nodes(self):
        nodes = self._read_nodes()
        if nodes is None:
            nodes = self._parse_nodes()
            if nodes is not None:
                self._dump_nodes(nodes)
        return nodes['child']

    def _read_nodes(self):
        sql = 'SELECT info FROM `nodes` WHERE nid=? LIMIT 1'
        req = cursor.execute(sql, (self.nid,))
        nodes = req.fetchone()
        if nodes is not None:
            print('local cache HIT!')
            return json.loads(nodes[0])
        return None

    def _parse_nodes(self):
        '''
        Get 50 nodes of this nid
        '''
        url = ''.join([
            QUKU,
            'op=query&fmt=json&src=mbox&cont=ninfo&rn=200&node=',
            str(self.nid),
            '&pn=0',
            ])
        print('_parse_node url:', url)
        req = request.urlopen(url)
        if req.status != 200:
            return None
        try:
            nodes = json.loads(req.read().decode())
        except Error as e:
            print(e)
            return None
        return nodes

    def _dump_nodes(self, nodes):
        sql = 'INSERT INTO `nodes` VALUES(?, ?, ?)'
        cursor.execute(sql, (self.nid, json.dumps(nodes),
            int(time.time())))
        conn.commit()


class TopList:
    '''
    Get the info of Top List songs.
    '''
    def __init__(self, nid):
        self.nid = nid

        self.init_tables()

    def init_tables(self):
        sql = '''
        CREATE TABLE IF NOT EXISTS `toplist` (
        nid INT,
        songs TEXT,
        timestamp INT
        )
        '''
        cursor.execute(sql)
        conn.commit()

    def get_songs(self):

        songs = self._read_songs()
        if songs is None:
            songs = self._parse_songs()
            if songs is not None:
                self._dump_songs(songs)
        return songs['musiclist']

    def _read_songs(self):
        sql = 'SELECT songs FROM `toplist` WHERE nid=? LIMIT 1'
        req = cursor.execute(sql, (self.nid, ))
        songs = req.fetchone()
        if songs is not None:
            print('local cache HIT!')
            return json.loads(songs[0])
        return None

    def _parse_songs(self):
        '''
        Get 50 songs of this top list.
        '''
        url = ''.join([
            TOPLIST,
            'from=pc&fmt=json&type=bang&data=content&rn=200&id=',
            str(self.nid),
            ])
        print('url-songs:', url)
        req = request.urlopen(url)
        if req.status != 200:
            return None
        try:
            songs = json.loads(req.read().decode())
        except Error as e:
            print(e)
            return None
        return songs

    def _dump_songs(self, songs):
        sql = 'INSERT INTO `toplist` VALUES(?, ?, ?)'
        cursor.execute(sql, (self.nid, json.dumps(songs), 
            int(time.time())))
        conn.commit()


class Song(GObject.GObject):
    '''
    Use Gobject to emit signals:
    register three signals: can-play and downloaded
    if `can-play` emited, player will receive a filename which have
    at least 1M to play.
    `chunk-received` signal is used to display the progressbar of 
    downloading process.
    `downloaded` signal may be used to popup a message to notify 
    user that a new song is downloaded.

    Remember to call Song.close() method when exit the process.
    '''
    __gsignals__ = {
            'can-play': (GObject.SIGNAL_RUN_LAST, 
                GObject.TYPE_NONE, (object, )),
            'chunk-received': (GObject.SIGNAL_RUN_LAST,
                GObject.TYPE_NONE, 
                (GObject.TYPE_UINT, GObject.TYPE_UINT)),
            'downloaded': (GObject.SIGNAL_RUN_LAST, 
                GObject.TYPE_NONE, (object, ))
            }
    def __init__(self, app):
        super().__init__()
        self.app = app

        self.conn = sqlite3.connect(conf['song-db'])
        self.cursor = self.conn.cursor()
        self.init_table()

    def close(self):
        self.conn.commit()
        self.conn.close()

    def init_table(self):
        self.cols = ['id', 'name', 'artist', 'album', 'rid', 'artistid',
                'albumid', 'filepath']
        sql = '''
        CREATE TABLE IF NOT EXISTS `song` (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name CHAR,
        artist CHAR,
        album CHAR,
        rid INTEGER,
        artistid INTEGER,
        albumid INTEGER,
        filepath CHAR
        )
        '''
        self.cursor.execute(sql)
        self.conn.commit()

    def play_song(self, song):
        '''
        Get the actual link of music file.
        If higher quality of that music unavailable, a lower one is used.
        like this:
        response=url&type=convert_url&format=ape|mp3&rid=MUSIC_3312608
        '''
        song_info = self._read_song_info(song['rid'])
        if song_info is not None:
            #emit can-play and downloaded signals.
            self.emit('can-play', song_info)
            self.emit('downloaded', song_info)
            return 

        # TODO: use async call:
        song_link = self._parse_song_link(song['rid'])
        print('song link:', song_link)

        if song_link is None:
            return None

        song_info = copy.copy(song)
        song_info['filepath'] = os.path.join(conf['song-dir'], 
                os.path.split(song_link)[1])
        self._write_song_info(song_info)
        self._download_song(song_link, song_info)
        return

    def append_playlist(self, song_info):
        print('append playlist')
        return

    def cache_song(self, song_info):
        print('cache song')
        return

    def _read_song_info(self, rid):
        sql = 'SELECT * FROM `song` WHERE rid=? LIMIT 1'
        req = self.cursor.execute(sql, (rid, ))
        song = req.fetchone()
        if song is not None:
            print('local song cache HIT!')
            song_info = dict(zip(self.cols , song))
            if os.path.exists(song_info['filepath']):
                return song_info
            else:
                self._delete_song_info(song_info)
                return None
        return None

    def _write_song_info(self, song_info):
        sql = '''INSERT INTO `song` (
                name, artist, album, rid, artistid, albumid, filepath
                ) VALUES(? , ?, ?, ?, ?, ?, ?)'''
        self.cursor.execute(sql, [song_info['name'], song_info['artist'], 
            song_info['album'], song_info['rid'], song_info['artistid'], 
            song_info['albumid'], song_info['filepath']])
        self.conn.commit()

    def _delete_song_info(self, song_info):
        sql = 'DELETE FROM `song` WHERE id=? LIMIT 1'
        self.cursor.execute(sql, (song_info['id'], ))
        self.conn.commit()

    def _parse_song_link(self, rid):
        if conf['use-ape']:
            _format = 'ape|mp3'
        else:
            _format = 'mp3'
        url = ''.join([
            SONG,
            'response=url&type=convert_url&format=',
            _format,
            '&rid=MUSIC_',
            rid,
            ])
        print('url-song-link:', url)
        req = request.urlopen(url)
        if req.status != 200:
            return None
        return req.read().decode()

    def _download_song(self, song_link, song_info):
        if os.path.exists(song_info['filepath']): 
            print('local song cache HIT!')
            self.emit('can-play', song_info)
            self.emit('downloaded', song_info)
            return
        req = request.urlopen(song_link)
        retrieved_size = 0
        can_play_emited = False
        content_length = req.headers.get('Content-Length')
        with open(song_info['filepath'], 'wb') as fh:
            while True:
                chunk = req.read(CHUNK)
                retrieved_size += len(chunk)
                # emit chunk-received signals
                # contains content_length and retrieved_size

                # check retrieved_size, and emit can-play signal.
                # this signal only emit once.
                if retrieved_size > CHUNK_TO_PLAY and not can_play_emited:
                    can_play_emited = True
                    self.emit('can-play', song_info)
                    print('song can be played now')
                if not chunk:
                    break
                fh.write(chunk)
            #emit downloaded signal.
            self.emit('downloaded', song_info)
            print('download finished')

# register Song to GObject
GObject.type_register(Song)
