
import copy
from gi.repository import GdkPixbuf
from gi.repository import GObject
import hashlib
import json
import leveldb
import os
import threading
import urllib.error
from urllib import parse
from urllib import request

from kuwo import Config
from kuwo import Utils

IMG_CDN = 'http://img4.kwcdn.kuwo.cn/'
ARTIST = 'http://artistlistinfo.kuwo.cn/mb.slist?'
QUKU = 'http://qukudata.kuwo.cn/q.k?'
QUKU_SONG = 'http://nplserver.kuwo.cn/pl.svc?'
SEARCH = 'http://search.kuwo.cn/r.s?'
SONG = 'http://antiserver.kuwo.cn/anti.s?'

CHUNK = 2 ** 14
CHUNK_TO_PLAY = 2 ** 21     # 2M
CHUNK_MV_TO_PLAY = 2 ** 23  # 8M
MAXTIMES = 3
TIMEOUT = 30

# Using weak reference to cache song list in TopList and Radio.
class Dict(dict):
    pass
req_cache = Dict()

# Using leveldb to cache urlrequest
ldb = leveldb.LevelDB(Config.CACHE_DB)

def empty_func(*args, **kwds):
    pass

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

def hash_byte(_str):
    return hashlib.sha512(_str.encode()).digest()

def hash_str(_str):
    return hashlib.sha1(_str.encode()).hexdigest()

def urlopen(_url, use_cache=True):
    # set host port from 81 to 80, to fix image problem
    url = _url.replace(':81', '')
    # hash the url to accelerate string compare speed in db.
    key = hash_byte(url)
    if use_cache:
        try:
            req = ldb.Get(key)
            return req
        except KeyError:
            req = None
    retried = 0
    while retried < MAXTIMES:
        try:
            req = request.urlopen(url, timeout=TIMEOUT)
            req_content = req.read()
            if use_cache:
                ldb.Put(key, req_content)
            return req_content
        except Exception as e:
            print('Error: Net.urlopen', e, 'url:', url)
            retried += 1
    if retried == MAXTIMES:
        return None

def get_nodes(nid):
    url = ''.join([
        QUKU,
        'op=query&fmt=json&src=mbox&cont=ninfo&rn=500&node=',
        str(nid),
        '&pn=0',
        ])
    req_content = urlopen(url)
    if req_content is None:
        return None
    try:
        nodes = json.loads(req_content.decode())
    except Exception as e:
        print('Error: Net.get_nodes:', e, 'with url:', url)
        return None
    return nodes['child']

def get_image(url):
    def _get_image(url): 
        req_content = urlopen(url, use_cache=False)
        if req_content is None:
            return None
        return req_content
    
    def _dump_image(image, filepath):
        with open(filepath, 'wb') as fh:
            fh.write(image)

    filename = os.path.split(url)[1]
    filepath = os.path.join(Config.IMG_DIR, filename)
    if os.path.exists(filepath):
        return filepath

    image = _get_image(url)
    if image is not None:
        _dump_image(image, filepath)
        return filepath
    return None

def get_album(albumid):
    url = ''.join([
        SEARCH,
        'stype=albuminfo&albumid=',
        str(albumid),
        ])
    req_content = urlopen(url)
    if req_content is None:
        return None
    try:
        songs_wrap = Utils.json_loads_single(req_content.decode())
    except Exception as e:
        print('Error: Net.get_album()', e, 'with url:', url)
        return None
    return songs_wrap

def update_liststore_image(liststore, path, col, url):
    def _update_image(filepath, error):
        if filepath is None:
            return
        try:
            pix = GdkPixbuf.Pixbuf.new_from_file_at_size(filepath, 100, 100)
            liststore[path][col] = pix
        except Exception as e:
            print('Error: Net.update_liststore_image:', e, 
                    'with filepath:', filepath)
    async_call(get_image, _update_image, url)

def update_album_covers(liststore, path, col, _url):
    url = _url.strip()
    if url and len(url) == 0:
        return None
    url = ''.join([
        IMG_CDN,
        'star/albumcover/',
        url,
        ])
    update_liststore_image(liststore, path, col, url)

def update_mv_image(liststore, path, col, _url):
    url = _url.strip()
    if url and len(url) == 0:
        return None
    url = ''.join([
        IMG_CDN,
        'wmvpic/',
        url,
        ])
    update_liststore_image(liststore, path, col, url)

def get_toplist_songs(nid):
    url = ''.join([
        'http://kbangserver.kuwo.cn/ksong.s?',
        'from=pc&fmt=json&type=bang&data=content&rn=200&id=',
        str(nid),
        ])
    if url not in req_cache:
        req_content = urlopen(url, use_cache=False)
        if req_content is None:
            return None
        req_cache[url] = req_content
    try:
        songs = json.loads(req_cache[url].decode())
    except Exception as e:
        print('Error: Net.get_toplist_songs:', e, 'with url:', url)
        return None
    return songs['musiclist']

def get_artists(catid, page, prefix):
    url = ''.join([
        ARTIST,
        'stype=artistlist&order=hot&rn=50&category=',
        str(catid),
        '&pn=',
        str(page),
        ])
    if len(prefix) > 0:
        url = url + '&prefix=' + prefix
    req_content = urlopen(url)
    if req_content is None:
        return None
    try:
        artists = Utils.json_loads_single(req_content.decode())
    except Exception as e:
        print('Error: Net.get_artists:', e, 'with url:', url)
        return None
    return artists

def update_toplist_node_logo(liststore, path, col, url):
    update_liststore_image(liststore, path, col, url)

def update_artist_logo(liststore, path, col, logo_id):
    if logo_id[:2] in ('55', '90',):
        logo_id = '100/' + logo_id[2:]
    url = ''.join([
        IMG_CDN,
        'star/starheads/',
        logo_id,
        ])
    update_liststore_image(liststore, path, col, url)

def get_artist_info(artistid):
    '''
    Get artist info, if cached, just return it.
    Artist pic is also retrieved and saved to info['pic']
    '''
    url = ''.join([SEARCH, 'stype=artistinfo&artistid=', str(artistid), ])
    req_content = urlopen(url)
    if req_content is None:
        return None
    try:
        info = Utils.json_loads_single(req_content.decode())
    except Exception as e:
        print('Error: Net.get_artist_info:', e, 'with url:', url)
        return None
    # set logo size to 100x100
    pic_path = info['pic']
    if pic_path[:2] in ('55', '90',):
        pic_path = '100/' + pic_path[2:]
    url = ''.join([IMG_CDN, 'star/starheads/', pic_path, ])
    info['pic'] = get_image(url)
    return info

def get_artist_songs(artist, page):
    '''
    Get 200 songs of this artist.
    '''
    url = ''.join([
        SEARCH,
        'ft=music&rn=200&itemset=newkw&newsearch=1&cluster=0',
        '&primitive=0&rformat=json&encoding=UTF8&artist=',
        parse.quote(artist, encoding='GBK'),
        '&pn=',
        str(page),
        ])
    req_content = urlopen(url)
    if req_content is None:
        return None
    try:
        songs = Utils.json_loads_single(req_content.decode())
    except Error as e:
        print('Error: Net.get_artist_songs:', e, 'with url:', url)
        return None
    return songs

def get_lrc(_rid):
    def _parse_lrc():
        url = ('http://newlyric.kuwo.cn/newlyric.lrc?' + 
                Utils.encode_lrc_url(rid))
        req_content = urlopen(url, use_cache=False)
        if req_content is None:
            return None
        try:
            lrc = Utils.decode_lrc_content(req_content)
        except Exception as e:
            print('Error: Net.get_lrc:', e, 'with url:', url)
            return None
        return lrc

    rid = str(_rid)
    filepath = os.path.join(Config.LRC_DIR, rid + '.lrc')
    if os.path.exists(filepath):
        with open(filepath) as fh:
            return fh.read()

    lrc = _parse_lrc()
    if lrc is not None:
        with open(filepath, 'w') as fh:
            fh.write(lrc)
        return lrc
    return None

def get_recommend_lists(artist):
    url = ''.join([
        'http://artistpicserver.kuwo.cn/pic.web?',
        'type=big_artist_pic&pictype=url&content=list&&id=0&from=pc',
        '&name=',
        Utils.encode_uri(artist),
        ])
    req_content = urlopen(url)
    if req_content is None:
        return None
    return req_content.decode()

def get_recommend_image(_url):
    '''
    Get big cover image about this artist, normally 1024x768
    '''
    def _get_image(url): 
        req_content = urlopen(url, use_cache=False)
        if req_content is None:
            return None
        return req_content

    url = _url.strip()
    if len(url) == 0:
        return None
    ext = os.path.splitext(url)[1]
    filename = hash_str(url) + ext
    filepath = os.path.join(Config.IMG_LARGE_DIR, filename)
    if os.path.exists(filepath):
        return filepath

    image = _get_image(url)
    if image is None:
        return None
    with open(filepath, 'wb') as fh:
        fh.write(image)
    return filepath

def search_songs(keyword, page=0):
    url = ''.join([
        SEARCH,
        'ft=music&rn=200&newsearch=1&primitive=0&cluster=0',
        '&itemset=newkm&rformat=json&encoding=utf8&all=',
        parse.quote(keyword),
        '&pn=',
        str(page),
        ])
    if url not in req_cache:
        req_content = urlopen(url, use_cache=False)
        if req_content is None:
            return None
        req_cache[url] = req_content
    try:
        songs_wrap = Utils.json_loads_single(req_cache[url].decode())
    except Exception as e:
        print('Error: Net.search_song:', e, 'with url:', url)
        return None
    return songs_wrap

def search_artists(keyword):
    url = ''.join([
        SEARCH,
        'ft=artist&pn=0&rn=500&newsearch=1&primitive=0&cluster=0',
        '&itemset=newkm&rformat=json&encoding=utf8&all=',
        parse.quote(keyword),
        ])
    if url not in req_cache:
        req_content = urlopen(url, use_cache=False)
        if req_content is None:
            return None
        req_cache[url] = req_content
    try:
        artists_wrap = Utils.json_loads_single(req_cache[url].decode())
    except Exception as e:
        print('Error: Net.search_artists():', e, 'with url:', url)
        return None
    return artists_wrap

def search_albums(keyword):
    url = ''.join([
        SEARCH,
        'ft=album&pn=0&rn=500&newsearch=1&primitive=0&cluster=0',
        '&itemset=newkm&rformat=json&encoding=utf8&all=',
        parse.quote(keyword),
        ])
    if url not in req_cache:
        req_content = urlopen(url, use_cache=False)
        if req_content is None:
            return None
        req_cache[url] = req_content
    try:
        albums_wrap = Utils.json_loads_single(req_cache[url].decode())
    except Exception as e:
        print('Error: Net.search_albums():', e, 'with url:', url)
        return None
    return albums_wrap

def get_index_nodes(nid):
    '''
    Get content of nodes from nid=2 to nid=15
    '''
    url = ''.join([
        QUKU,
        'op=query&fmt=json&src=mbox&cont=ninfo&rn=500&node=',
        str(nid),
        '&pn=0',
        ])
    req_content = urlopen(url)
    if req_content is None:
        return None
    try:
        nodes_wrap = json.loads(req_content.decode())
    except Error as e:
        print('Error: Net.get_index_nodes():', e, 'with url:', url)
        return None
    return nodes_wrap

def get_themes_main():
    def append_to_nodes(nid, use_child=True):
        node_wrap = get_index_nodes(nid)
        if node_wrap is None:
            return None
        if use_child:
            # node is limited to 10, no more are needed.
            for node in node_wrap['child'][:10]:
                nodes.append({
                    'name': node['disname'],
                    'nid': int(node['id']),
                    'info': node['info'],
                    'pic': node['pic'],
                    })
        else:
            # Because of different image style, we use child picture instaed
            node = node_wrap['ninfo']
            pic = node_wrap['child'][0]['pic']
            nodes.append({
                'name': node['disname'],
                'nid': int(node['id']),
                'info': node['info'],
                'pic': pic,
                })

    nodes = []
    # 语言 10(+)
    append_to_nodes(10)
    # Test:
    # 人群 11
    append_to_nodes(11, False)
    # 节日 12
    append_to_nodes(12, False)
    # 心情 13(+)
    append_to_nodes(13)
    # 场景 14
    append_to_nodes(14, False)
    # 曲风流派 15(+)
    append_to_nodes(15)
    # 时间 72325
    append_to_nodes(72325, False)
    # 环境 72326
    append_to_nodes(72326, False)
    # 精选集 22997 这个格式不正确, 不要了.
    #append_to_nodes(22997, False)
    if len(nodes) > 0:
        return nodes
    else:
        return None

def get_themes_sub(nid):
    return get_nodes(nid)

def get_themes_songs(nid, page):
    url = ''.join([
        QUKU_SONG,
        'op=getlistinfo&rn=200&encode=utf-8&identity=kuwo&keyset=pl2012',
        '&pn=',
        str(page),
        '&pid=',
        str(nid),
        ])
    if url not in req_cache:
        req_content = urlopen(url, use_cache=False)
        if req_content is None:
            return None
        req_cache[url] = req_content
    try:
        songs_wrap = json.loads(req_cache[url].decode())
    except Exception as e:
        print('Error: Net.get_themes_songs():', e, 'with url:', url)
        return None
    return songs_wrap

def get_mv_songs(pid):
    url = ''.join([
        QUKU_SONG,
        'op=getlistinfo&pn=0&rn=1000&encode=utf-8&keyset=mvpl&pid=',
        str(pid),
        ])
    req_content = urlopen(url)
    if req_content is None:
        return None
    try:
        songs_wrap = json.loads(req_content.decode())
    except Exception as e:
        print('Error: Net.get_mv_songs():', e, 'with url:', url)
        return None
    return songs_wrap

def get_radios_nodes():
    nid = 8
    return get_nodes(nid)

def get_radio_songs(nid, offset):
    url = ''.join([
        'http://gxh2.kuwo.cn/newradio.nr?',
        'type=4&uid=0&login=0&size=20&fid=',
        str(nid),
        '&offset=',
        str(offset),
        ])
    req_content = urlopen(url)
    if req_content is None:
        return None
    songs = Utils.parse_radio_songs(req_content.decode('gbk'))
    return songs

def get_song_link(rid, high_res=False, use_mv=False):
    if use_mv:
        _format = 'mkv|mp4' if high_res else 'mp4'
    else:
        _format = 'ape|mp3' if high_res else 'mp3'
    url = ''.join([
        SONG,
        'response=url&type=convert_url&format=',
        _format,
        '&rid=MUSIC_',
        str(rid),
        ])
    req_content = urlopen(url)
    if req_content is None:
        return None
    song_link = req_content.decode()
    if len(song_link) < 20:
        return None
    song_list = song_link.split('/')
    song_link = '/'.join(song_list[:3] + song_list[5:])
    return song_link


class AsyncSong(GObject.GObject):
    '''
    Use Gobject to emit signals:
    register three signals: can-play and downloaded
    if `can-play` emited, player will receive a filename which have
    at least 1M to play.
    `chunk-received` signal is used to display the progressbar of 
    downloading process.
    `downloaded` signal may be used to popup a message to notify 
    user that a new song is downloaded.
    '''
    __gsignals__ = {
            'can-play': (GObject.SIGNAL_RUN_LAST, 
                GObject.TYPE_NONE, (object, )),
            #'chunk-received': (GObject.SIGNAL_RUN_LAST,
            #    GObject.TYPE_NONE, 
            #    (object, int)),
            'downloaded': (GObject.SIGNAL_RUN_LAST, 
                GObject.TYPE_NONE, (object, ))
            }
    def __init__(self, app):
        super().__init__()
        self.app = app

    def get_song(self, song):
        '''
        Get the actual link of music file.
        If higher quality of that music unavailable, a lower one is used.
        like this:
        response=url&type=convert_url&format=ape|mp3&rid=MUSIC_3312608
        '''
        if 'filepath' in song and os.path.exists(song['filepath']): 
            print('local song exists, signals will be emited')
            self.emit('can-play', song)
            self.emit('downloaded', song)
            return
        async_call(self._download_song, empty_func, song)

    def _download_song(self, song):
        def _wrap(req):
            received_size = 0
            can_play_emited = False
            content_length = int(req.headers.get('Content-Length'))
            print('size of file: ', round(content_length / 2**20, 2), 'M')
            with open(song['filepath'], 'wb') as fh:
                while True:
                    chunk = req.read(CHUNK)
                    received_size += len(chunk)
                    percent = int(received_size/content_length * 100)
                    print('percent:', percent)
                    # check retrieved_size, and emit can-play signal.
                    # this signal only emit once.
                    if (received_size > CHUNK_TO_PLAY or percent > 40) \
                            and not can_play_emited:
                        print('song can be played now')
                        can_play_emited = True
                        self.emit('can-play', song)
                    if not chunk:
                        break
                    fh.write(chunk)
                #emit downloaded signal.
                self.emit('downloaded', song)

        song_link = get_song_link(song['rid'], self.app.conf['use-ape'])
        if song_link is None:
            return None

        song['filepath'] = os.path.join(self.app.conf['song-dir'], 
                os.path.split(song_link)[1])

        retried = 0
        while retried < MAXTIMES:
            try:
                req = request.urlopen(song_link)
                _wrap(req)
                return song
            except Exception as e:
                print('AsyncSong._download_song()', e, 'with song_link:',
                        song_link)
                retried += 1
        # remember to check song when `downloaded` signal received.
        if retried == MAXTIMES:
            print('song failed to download, please check link', song_link)
            #self.emit('downloaded', None)
            return None
GObject.type_register(AsyncSong)


class AsyncMV(GObject.GObject):
    __gsignals__ = {
            'can-play': (GObject.SIGNAL_RUN_LAST, 
                GObject.TYPE_NONE, (str, )),
            'downloaded': (GObject.SIGNAL_RUN_LAST, 
                GObject.TYPE_NONE, (str, )),
            }
    def __init__(self, app):
        super().__init__()
        self.app = app

    def get_mv(self, mv_link):
        mv_path = os.path.join(self.app.conf['mv-dir'],
                os.path.split(mv_link)[1])

        if os.path.exists(mv_path):
            self.emit('can-play', mv_path)
            self.emit('downloaded', mv_path)
            return mv_path
        async_call(self._download_mv, empty_func, mv_link, mv_path)

    def _download_mv(self, mv_link, mv_path):
        def _wrap(req):
            received_size = 0
            can_play_emited = False
            content_length = int(req.headers.get('Content-Length'))
            print('size of file: ', round(content_length / 2**20, 2), 'M')
            with open(mv_path, 'wb') as fh:
                while True:
                    chunk = req.read(CHUNK)
                    received_size += len(chunk)
                    percent = int(received_size/content_length * 100)
                    print('percent:', percent)
                    if (received_size > CHUNK_MV_TO_PLAY or percent > 20) \
                            and not can_play_emited:
                        can_play_emited = True
                        self.emit('can-play', mv_path)
                    if not chunk:
                        break
                    fh.write(chunk)
                self.emit('downloaded', mv_path)
        retried = 0
        while retried < MAXTIMES:
            try:
                req = request.urlopen(mv_link)
                _wrap(req)
                return mv_path
            except Exception as e:
                print('AsyncMV.getmv()', e, 'with mv_link:', mv_link)
                retried += 1
        if retried == MAXTIMES:
            print('mv failed to download, please check link', mv_link)
            #self.emit('downloaded', None)
            return None
GObject.type_register(AsyncMV)
