#!/usr/bin/env python3
#
# Bookmark management utility
#
# Copyright © 2015-2017 Arun Prakash Jana <engineerarun@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Buku.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import html.parser as HTMLParser
import json
import logging
import os
import re
try:
    import readline
    readline
except ImportError:
    pass
import requests
import signal
import sqlite3
import sys
import threading
import urllib3
from urllib3.util import parse_url, make_headers
import webbrowser

__version__ = '2.8'
__author__ = 'Arun Prakash Jana <engineerarun@gmail.com>'
__license__ = 'GPLv3'

# Global variables
interrupted = False  # Received SIGINT
DELIM = ','  # Delimiter used to store tags in DB
SKIP_MIMES = {'.pdf', '.txt'}
colorize = True  # Allow color output by default

# Default colour to print records
ID_str = '\x1b[1m\x1b[93m%d. \x1b[0m\x1b[92m%s\x1b[0m \x1b[1m[%s]\x1b[0m\n'
ID_DB_str = '\x1b[1m\x1b[93m%d. \x1b[0m\x1b[92m%s\x1b[0m'
MUTE_str = '%s \x1b[1m(L)\x1b[0m\n'
TITLE_str = '%s   \x1b[91m>\x1b[0m %s\n'
DESC_str = '%s   \x1b[91m+\x1b[0m %s\n'
TAG_str = '%s   \x1b[91m#\x1b[0m %s\n'

# Disguise as Firefox on Ubuntu
USER_AGENT = 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:50.0) Gecko/20100101 \
Firefox/50.0'
myheaders = None  # Default dictionary of headers
myproxy = None  # Default proxy

# Set up logging
logging.basicConfig(format='[%(levelname)s] %(message)s')
logger = logging.getLogger()
logdbg = logger.debug
logerr = logger.error


class BukuHTMLParser(HTMLParser.HTMLParser):
    '''Class to parse and fetch the title
    from a HTML page, if available
    '''

    def __init__(self):
        HTMLParser.HTMLParser.__init__(self)
        self.in_title_tag = False
        self.data = ''
        self.prev_tag = None
        self.parsed_title = None

    def handle_starttag(self, tag, attrs):
        self.in_title_tag = False
        if tag == 'title':
            self.in_title_tag = True
            self.prev_tag = tag

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title_tag = False
            if self.data != '':
                self.parsed_title = self.data
                self.reset()  # We have received title data, exit parsing

    def handle_data(self, data):
        if self.prev_tag == 'title' and self.in_title_tag:
            self.data = '%s%s' % (self.data, data)

    def error(self, message):
        pass


class BukuCrypt:
    '''Class to handle encryption and decryption of
    the database file. Functionally a separate entity.

    Involves late imports in the static functions but it
    saves ~100ms each time. Given that encrypt/decrypt are
    not done automatically and any one should be called at
    a time, this doesn't seem to be an outrageous approach.
    '''

    # Crypto constants
    BLOCKSIZE = 0x10000  # 64 KB blocks
    SALT_SIZE = 0x20
    CHUNKSIZE = 0x80000  # Read/write 512 KB chunks

    @staticmethod
    def get_filehash(filepath):
        '''Get the SHA256 hash of a file

        :param filepath: path to the file
        :return: hash digest of the file
        '''

        from hashlib import sha256

        with open(filepath, 'rb') as fp:
            hasher = sha256()
            buf = fp.read(BukuCrypt.BLOCKSIZE)
            while len(buf) > 0:
                hasher.update(buf)
                buf = fp.read(BukuCrypt.BLOCKSIZE)

            return hasher.digest()

    @staticmethod
    def encrypt_file(iterations, dbfile=None):
        '''Encrypt the bookmarks database file

        :param iterations: number of iterations for key generation
        :param dbfile: custom database file path (including filename)
        '''

        try:
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives.ciphers import (Cipher, modes,
                                                                algorithms)
            from getpass import getpass
            from hashlib import sha256
            import struct
        except ImportError:
            logerr('cryptography lib(s) missing')
            sys.exit(1)

        if iterations < 1:
            logerr('Iterations must be >= 1')
            sys.exit(1)

        if not dbfile:
            dbfile = os.path.join(BukuDb.get_default_dbdir(), 'bookmarks.db')
        encfile = '%s.enc' % dbfile

        db_exists = os.path.exists(dbfile)
        enc_exists = os.path.exists(encfile)

        if db_exists and not enc_exists:
            pass
        elif not db_exists:
            logerr('%s missing. Already encrypted?', dbfile)
            sys.exit(1)
        else:
            # db_exists and enc_exists
            logerr('Both encrypted and flat DB files exist!')
            sys.exit(1)

        password = getpass()
        passconfirm = getpass()
        if not password or not passconfirm:
            logerr('Empty password')
            sys.exit(1)
        if password != passconfirm:
            logerr('Passwords do not match')
            sys.exit(1)

        try:
            # Get SHA256 hash of DB file
            dbhash = BukuCrypt.get_filehash(dbfile)
        except Exception as e:
            logerr(e)
            sys.exit(1)

        # Generate random 256-bit salt and key
        salt = os.urandom(BukuCrypt.SALT_SIZE)
        key = ('%s%s' % (password,
               salt.decode('utf-8', 'replace'))).encode('utf-8')
        for _ in range(iterations):
            key = sha256(key).digest()

        iv = os.urandom(16)
        encryptor = Cipher(
            algorithms.AES(key),
            modes.CBC(iv),
            backend=default_backend()
        ).encryptor()
        filesize = os.path.getsize(dbfile)

        try:
            with open(dbfile, 'rb') as infp, open(encfile, 'wb') as outfp:
                outfp.write(struct.pack('<Q', filesize))
                outfp.write(salt)
                outfp.write(iv)

                # Embed DB file hash in encrypted file
                outfp.write(dbhash)

                while True:
                    chunk = infp.read(BukuCrypt.CHUNKSIZE)
                    if len(chunk) == 0:
                        break
                    elif len(chunk) % 16 != 0:
                        chunk = '%s%s' % (chunk, ' ' * (16 - len(chunk) % 16))

                    outfp.write(encryptor.update(chunk) + encryptor.finalize())

            os.remove(dbfile)
            print('File encrypted')
            sys.exit(0)
        except Exception as e:
            logerr(e)
            sys.exit(1)

    @staticmethod
    def decrypt_file(iterations, dbfile=None):
        '''Decrypt the bookmarks database file

        :param iterations: number of iterations for key generation
        :param dbfile: custom database file path (including filename)
        :              The '.enc' suffix must be omitted.
        '''

        try:
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives.ciphers import (Cipher, modes,
                                                                algorithms)
            from getpass import getpass
            from hashlib import sha256
            import struct
        except ImportError:
            logerr('cryptography lib(s) missing')
            sys.exit(1)

        if iterations < 1:
            logerr('Decryption failed')
            sys.exit(1)

        if not dbfile:
            dbfile = os.path.join(BukuDb.get_default_dbdir(), 'bookmarks.db')
        else:
            dbfile = os.path.abspath(dbfile)
            dbpath, filename = os.path.split(dbfile)

        encfile = '%s.enc' % dbfile

        enc_exists = os.path.exists(encfile)
        db_exists = os.path.exists(dbfile)

        if enc_exists and not db_exists:
            pass
        elif not enc_exists:
            logerr('%s missing', encfile)
            sys.exit(1)
        else:
            # db_exists and enc_exists
            logerr('Both encrypted and flat DB files exist!')
            sys.exit(1)

        password = getpass()
        if not password:
            logerr('Decryption failed')
            sys.exit(1)

        try:
            with open(encfile, 'rb') as infp:
                size = struct.unpack('<Q', infp.read(struct.calcsize('Q')))[0]

                # Read 256-bit salt and generate key
                salt = infp.read(32)
                key = ('%s%s' % (password,
                       salt.decode('utf-8', 'replace'))).encode('utf-8')
                for _ in range(iterations):
                    key = sha256(key).digest()

                iv = infp.read(16)
                decryptor = Cipher(
                    algorithms.AES(key),
                    modes.CBC(iv),
                    backend=default_backend(),
                ).decryptor()

                # Get original DB file's SHA256 hash from encrypted file
                enchash = infp.read(32)

                with open(dbfile, 'wb') as outfp:
                    while True:
                        chunk = infp.read(BukuCrypt.CHUNKSIZE)
                        if len(chunk) == 0:
                            break

                        outfp.write(
                                decryptor.update(chunk) + decryptor.finalize())

                    outfp.truncate(size)

            # Match hash of generated file with that of original DB file
            dbhash = BukuCrypt.get_filehash(dbfile)
            if dbhash != enchash:
                os.remove(dbfile)
                logerr('Decryption failed')
                sys.exit(1)
            else:
                os.remove(encfile)
                print('File decrypted')
        except struct.error:
            logerr('Tainted file')
            sys.exit(1)
        except Exception as e:
            logerr(e)
            sys.exit(1)


class BukuDb:
    '''Abstracts all database operations'''

    def __init__(self, json=False, field_filter=0, chatty=False, dbfile=None,
                 colorize=True):
        '''Database initialization API

        :param json: print results in json format
        :param field_filter: bookmark print format specifier
        :param chatty: set the verbosity of the APIs
        :param dbfile: custom database file path (including filename)
        :param colorize: use colour in output
        '''

        self.conn, self.cur = BukuDb.initdb(dbfile)
        self.json = json
        self.field_filter = field_filter
        self.chatty = chatty
        self.colorize = colorize

    @staticmethod
    def get_default_dbdir():
        '''Determine the directory path where dbfile will be stored:
        if $XDG_DATA_HOME is defined, use it
        else if $HOME exists, use it
        else use the current directory

        :return: path to database file
        '''

        data_home = os.environ.get('XDG_DATA_HOME')
        if data_home is None:
            if os.environ.get('HOME') is None:
                return os.path.abspath('.')
            else:
                data_home = os.path.join(os.environ.get('HOME'),
                                         '.local', 'share')

        return os.path.join(data_home, 'buku')

    @staticmethod
    def initdb(dbfile=None):
        '''Initialize the database connection. Create DB
        file and/or bookmarks table if they don't exist.
        Alert on encryption options on first execution.

        :param dbfile: custom database file path (including filename)
        :return: (connection, cursor) tuple
        '''

        if not dbfile:
            dbpath = BukuDb.get_default_dbdir()
            filename = 'bookmarks.db'
            dbfile = os.path.join(dbpath, filename)
        else:
            dbfile = os.path.abspath(dbfile)
            dbpath, filename = os.path.split(dbfile)

        encfile = dbfile + '.enc'

        try:
            if not os.path.exists(dbpath):
                os.makedirs(dbpath)
        except Exception as e:
            logerr(e)
            os.exit(1)

        db_exists = os.path.exists(dbfile)
        enc_exists = os.path.exists(encfile)

        if db_exists and not enc_exists:
            pass
        elif enc_exists and not db_exists:
            logerr('Unlock database first')
            sys.exit(1)
        elif db_exists and enc_exists:
            logerr('Both encrypted and flat DB files exist!')
            sys.exit(1)
        else:
            # not db_exists and not enc_exists
            print('DB file is being created at %s.\nYou should encrypt it.'
                  % dbfile)

        try:
            # Create a connection
            conn = sqlite3.connect(dbfile, check_same_thread=False)
            conn.create_function('REGEXP', 2, regexp)
            cur = conn.cursor()

            # Create table if it doesn't exist
            cur.execute('CREATE TABLE if not exists bookmarks \
                        (id integer PRIMARY KEY, URL text NOT NULL UNIQUE, \
                        metadata text default \'\', tags text default \',\', \
                        desc text default \'\', flags integer default 0)')
            conn.commit()
        except Exception as e:
            logerr('initdb(): %s', e)
            sys.exit(1)

        # Add description column in existing DB (from version 2.1)
        try:
            query = 'ALTER TABLE bookmarks ADD COLUMN desc text default \'\''
            cur.execute(query)
            conn.commit()
        except Exception:
            pass

        '''Add flags column in existing DB
        Introduced in v2.7 to handle immutable title
        Designed to be extended in future using bitwise masks
        Masks:
        0b00000001: set title immutable'''
        try:
            query = 'ALTER TABLE bookmarks ADD COLUMN flags integer default 0'
            cur.execute(query)
            conn.commit()
        except Exception:
            pass

        return (conn, cur)

    def get_rec_by_id(self, index):
        '''Get a bookmark from database by its ID.

        :return: bookmark data as a tuple, or None, if index is not found
        '''

        self.cur.execute('SELECT * FROM bookmarks WHERE id = ?', (index,))
        results = self.cur.fetchall()
        if len(results) == 0:
            return None
        else:
            return results[0]

    def get_rec_id(self, url):
        '''Check if URL already exists in DB

        :param url: URL to search
        :return: DB index if URL found, else -1
        '''

        self.cur.execute('SELECT id FROM bookmarks WHERE URL = ?', (url,))
        resultset = self.cur.fetchall()
        if len(resultset) == 0:
            return -1

        return resultset[0][0]

    def add_rec(self, url, title_in=None, tags_in=None, desc=None, immutable=0,
                delay_commit=False):
        '''Add a new bookmark

        :param url: URL to bookmark
        :param title_in: string title to add manually
        :param tags_in: string of comma-separated tags to add manually
                        must start and end with comma
        :param desc: string description
        :param immutable: disable title fetch from web
        :param delay_commit: do not commit to DB, caller responsibility
        :return: True on success, False on failure
        '''

        # Return error for empty URL
        if not url or url == '':
            logerr('Invalid URL')
            return False

        # Ensure that the URL does not exist in DB already
        id = self.get_rec_id(url)
        if id != -1:
            logerr('URL [%s] already exists at index %d', url, id)
            return False

        # Process title
        if title_in is not None:
            meta = title_in
        else:
            meta, mime, bad = network_handler(url)
            if bad:
                print('Malformed URL\n')
            elif mime:
                logdbg('Mime HEAD requested')
            elif meta == '':
                print('No title\n')
            else:
                logdbg('Title: [%s]', meta)

        # Process tags
        if tags_in is None or tags_in == '':
            tags_in = DELIM
        else:
            if tags_in[0] != DELIM:
                tags_in = '%s%s' % (DELIM, tags_in)
            if tags_in[-1] != DELIM:
                tags_in = '%s%s' % (tags_in, DELIM)

        # Process description
        if desc is None:
            desc = ''

        try:
            flagset = 0
            if immutable == 1:
                flagset |= immutable

            query = 'INSERT INTO bookmarks(URL, metadata, tags, desc, flags) \
                    VALUES (?, ?, ?, ?, ?)'
            self.cur.execute(query, (url, meta, tags_in, desc, flagset))
            if not delay_commit:
                self.conn.commit()
            if self.chatty:
                self.print_rec(self.cur.lastrowid)
            return True
        except Exception as e:
            logerr('add_rec(): %s', e)
            return False

    def append_tag_at_index(self, index, tags_in):
        '''Append tags for bookmark at index

        :param index: int position of record, 0 for all
        :param tags_in: string of comma-separated tags to add manually
        :return: True on success, False on failure
        '''

        if index == 0:
            resp = input('Append specified tags to ALL bookmarks? (y/n): ')
            if resp != 'y':
                return False

            self.cur.execute('SELECT id, tags FROM bookmarks ORDER BY id ASC')
        else:
            self.cur.execute('SELECT id, tags FROM bookmarks WHERE id = ?',
                             (index,))

        resultset = self.cur.fetchall()
        query = 'UPDATE bookmarks SET tags = ? WHERE id = ?'
        for row in resultset:
            tags = '%s%s' % (row[1], tags_in[1:])
            tags = parse_tags([tags])
            self.cur.execute(query, (tags, row[0],))
            if self.chatty:
                self.print_rec(row[0])

        self.conn.commit()
        return True

    def delete_tag_at_index(self, index, tags_in):
        '''Delete tags for bookmark at index

        :param index: int position of record, 0 for all
        :param tags_in: string of comma-separated tags to delete manually
        :return: True on success, False on failure
        '''

        tags_to_delete = tags_in.strip(DELIM).split(DELIM)

        if index == 0:
            resp = input('Delete specified tag(s) from ALL bookmarks? (y/n): ')
            if resp != 'y':
                return False

            count = 0
            match = "'%' || ? || '%'"
            for tag in tags_to_delete:
                q = "UPDATE bookmarks SET tags = replace(tags, '%s%s%s', '%s')\
                     WHERE tags LIKE %s" % (DELIM, tag, DELIM, DELIM, match)
                self.cur.execute(q, (DELIM + tag + DELIM,))
                count += self.cur.rowcount

            if count:
                self.conn.commit()
                if self.chatty:
                    print('%d records updated' % count)
        else:
            query = 'SELECT id, tags FROM bookmarks WHERE id = ?'
            self.cur.execute(query, (index,))
            resultset = self.cur.fetchall()

            query = 'UPDATE bookmarks SET tags = ? WHERE id = ?'
            for row in resultset:
                tags = row[1]

                for tag in tags_to_delete:
                    tags = tags.replace('%s%s%s' % (DELIM, tag, DELIM,), DELIM)

                self.cur.execute(query, (parse_tags([tags]), row[0],))
                if self.chatty:
                    self.print_rec(row[0])

                self.conn.commit()

        return True

    def update_rec(self, index, url=None, title_in=None, tags_in=None,
                   desc=None, immutable=-1, threads=4):
        '''Update an existing record at index
        Update all records if index is 0 and url is not specified.
        URL is an exception because URLs are unique in DB.

        :param index: int position to update, 0 for all
        :param url: bookmark address
        :param title_in: string title to add manually
        :param tags_in: string of comma-separated tags to add manually
                        must start and end with comma
                        prefix with '+,' to append to current tags
                        prefix with '-,' to delete from current tags
        :param desc: string description
        :param immutable: disable title fetch from web, if 1
        :param threads: number of threads to use to refresh full DB
        :return: True on success, False on failure
        '''

        arguments = []
        query = 'UPDATE bookmarks SET'
        to_update = False
        tag_modified = False
        ret = False

        # Update URL if passed as argument
        if url is not None and url != '':
            if index == 0:
                logerr('All URLs cannot be same')
                return False
            query = '%s URL = ?,' % query
            arguments += (url,)
            to_update = True

        # Update tags if passed as argument
        if tags_in is not None:
            if tags_in == '+,' or tags_in == '-,':
                logerr('Please specify a tag')
                return False

            if tags_in.startswith('+,'):
                ret = self.append_tag_at_index(index, tags_in[1:])
                tag_modified = True
            elif tags_in.startswith('-,'):
                ret = self.delete_tag_at_index(index, tags_in[1:])
                tag_modified = True
            else:
                query = '%s tags = ?,' % query
                arguments += (tags_in,)
                to_update = True

        # Update description if passed as an argument
        if desc is not None:
            query = '%s desc = ?,' % query
            arguments += (desc,)
            to_update = True

        # Update immutable flag if passed as argument
        if immutable != -1:
            flagset = 1
            if immutable == 1:
                query = '%s flags = flags | ?,' % query
            elif immutable == 0:
                query = '%s flags = flags & ?,' % query
                flagset = ~flagset

            arguments += (flagset,)
            to_update = True

        # Update title
        #
        # 1. if -t has no arguments, delete existing title
        # 2. if -t has arguments, update existing title
        # 3. if -t option is omitted at cmdline:
        #    if URL is passed, update the title from web using the URL
        # 4. if no other argument (url, tag, comment, immutable) passed,
        #    update title from web using DB URL (if title is mutable)
        title_to_insert = None
        if title_in is not None:
            title_to_insert = title_in
        elif url is not None and url != '':
            title_to_insert, mime, bad = network_handler(url)
            if bad:
                print('Malformed URL\n')
            elif mime:
                logdbg('Mime HEAD requested')
            elif title_to_insert == '':
                print('No title\n')
            else:
                logdbg('Title: [%s]', title_to_insert)
        elif not to_update and not tag_modified:
            ret = self.refreshdb(index, threads)
            if ret and index and self.chatty:
                self.print_rec(index)
            return ret

        if title_to_insert is not None:
            query = '%s metadata = ?,' % query
            arguments += (title_to_insert,)
            to_update = True

        if not to_update:       # Nothing to update
            return ret

        if index == 0:  # Update all records
            resp = input('Update ALL bookmarks? (y/n): ')
            if resp != 'y':
                return False

            query = query[:-1]
        else:
            query = '%s WHERE id = ?' % query[:-1]
            arguments += (index,)

        logdbg('query: "%s", args: %s', query, arguments)

        try:
            self.cur.execute(query, arguments)
            self.conn.commit()
            if self.cur.rowcount and self.chatty:
                self.print_rec(index)

            if self.cur.rowcount == 0:
                logerr('No matching index %d', index)
                return False
        except sqlite3.IntegrityError:
            logerr('URL already exists')
            return False

        return True

    def refreshdb(self, index, threads):
        '''Refresh ALL records in the database. Fetch title for each
        bookmark from the web and update the records. Doesn't update
        the record if title is empty.
        This API doesn't change DB index, URL or tags of a bookmark.
        This API is verbose.
        :param index: index of record to update, or 0 for all records
        '''

        if index == 0:
            self.cur.execute('SELECT id, url FROM bookmarks WHERE \
                             flags & 1 != 1 ORDER BY id ASC')
        else:
            self.cur.execute('SELECT id, url FROM bookmarks WHERE id = ? AND \
                             flags & 1 != 1', (index,))

        resultset = self.cur.fetchall()
        recs = len(resultset)
        if not recs:
            logerr('No matching index or title immutable or empty DB')
            return False

        # Set up strings to be printed
        if self.colorize:
            bad_url_str = '\x1b[1mIndex %d: Malformed URL\x1b[0m\n'
            mime_str = '\x1b[1mIndex %d: Mime HEAD requested\x1b[0m\n'
            blank_title_str = '\x1b[1mIndex %d: No title\x1b[0m\n'
            success_str = 'Title: [%s]\n\x1b[92mIndex %d: updated\x1b[0m\n'
        else:
            bad_url_str = 'Index %d: Malformed URL\n'
            mime_str = 'Index %d: Mime HEAD requested\n'
            blank_title_str = 'Index %d: No title\n'
            success_str = 'Title: [%s]\nIndex %d: updated\n'

        query = 'UPDATE bookmarks SET metadata = ? WHERE id = ?'
        done = {'value': 0}  # count threads completed
        processed = {'value': 0}  # count number of records processed

        # An additional call to generate default headers
        # gen_headers() is called within network_handler()
        # However, this initial call to setup headers
        # ensures there is no race condition among the
        # initial threads to setup headers
        if not myheaders:
            gen_headers()

        cond = threading.Condition()
        cond.acquire()

        def refresh(count, cond):
            '''Inner function to fetch titles and update records

            param count: dummy input to adhere to convention
            param cond: threading condition object
            '''

            count = 0

            while True:
                cond.acquire()
                if len(resultset) > 0:
                    row = resultset.pop()
                else:
                    cond.release()
                    break
                cond.release()

                title, mime, bad = network_handler(row[1])
                count += 1

                cond.acquire()
                if bad:
                    print(bad_url_str % row[0])
                    cond.release()
                    continue
                elif mime:
                    print(mime_str % row[0])
                    cond.release()
                    continue
                elif title == '':
                    print(blank_title_str % row[0])
                    cond.release()
                    continue

                self.cur.execute(query, (title, row[0],))
                # Save after fetching 32 titles per thread
                if count & 0b11111 == 0:
                    self.conn.commit()

                if self.chatty:
                    print(success_str % (title, row[0]))
                cond.release()

                if interrupted:
                    break

            logdbg('Thread %d: processed %d', threading.get_ident(), count)
            with cond:
                done['value'] += 1
                processed['value'] += count
                cond.notify()

        if recs < threads:
            threads = recs

        for i in range(threads):
            thread = threading.Thread(target=refresh, args=(i, cond))
            thread.start()

        while done['value'] < threads:
            cond.wait()
            logdbg('%d threads completed', done['value'])

        # Guard: records found == total records processed
        if recs != processed['value']:
            logerr('Records: %d, processed: %d !!!', recs, processed['value'])

        cond.release()
        self.conn.commit()
        return True

    def searchdb(self, keywords, all_keywords=False, deep=False, regex=False):
        '''Search the database for an entries with tags or URL
        or title info matching keywords and list those.

        :param keywords: keywords to search
        :param all_keywords: search any or all keywords
        :param deep: search for matching substrings
        :param regex: match a regular expression
        :return: search results, or None, if no matches
        '''

        qry = 'SELECT id, url, metadata, tags, desc FROM bookmarks WHERE'
        # Deep query string
        q1 = "(tags LIKE ('%' || ? || '%') OR URL LIKE ('%' || ? || '%') OR \
             metadata LIKE ('%' || ? || '%') OR desc LIKE ('%' || ? || '%'))"
        # Non-deep query string
        q2 = '(tags REGEXP ? OR URL REGEXP ? OR metadata REGEXP ? OR desc \
             REGEXP ?)'
        qargs = []

        if regex:
            for token in keywords:
                qry = '%s %s OR' % (qry, q2)

                qargs += (token, token, token, token,)
            qry = qry[:-3]
        elif all_keywords:
            if len(keywords) == 1 and keywords[0] == 'blank':
                qry = "SELECT * FROM bookmarks WHERE metadata = '' OR tags = ?"
                qargs += (DELIM,)
            elif len(keywords) == 1 and keywords[0] == 'immutable':
                qry = 'SELECT * FROM bookmarks WHERE flags & 1 == 1'
            else:
                for token in keywords:
                    if deep:
                        qry = '%s %s AND' % (qry, q1)
                    else:
                        token = '\\b' + token.rstrip('/') + '\\b'
                        qry = '%s %s AND' % (qry, q2)

                    qargs += (token, token, token, token,)
                qry = qry[:-4]
        elif not all_keywords:
            for token in keywords:
                if deep:
                    qry = '%s %s OR' % (qry, q1)
                else:
                    token = '\\b' + token.rstrip('/') + '\\b'
                    qry = '%s %s OR' % (qry, q2)

                qargs += (token, token, token, token,)
            qry = qry[:-3]
        else:
            logerr('Invalid search option')
            return None

        qry = '%s ORDER BY id ASC' % qry
        logdbg('query: "%s", args: %s', qry, qargs)

        try:
            self.cur.execute(qry, qargs)
        except sqlite3.OperationalError as e:
            logerr(e)
            return None

        results = self.cur.fetchall()
        if len(results) == 0:
            return None

        return results

    def search_by_tag(self, tag):
        '''Search and list bookmarks with a tag

        :param tag: a tag to search as string
        :return: search results, or None, if no matches
        '''

        tag = '%s%s%s' % (DELIM, tag.strip(DELIM), DELIM)
        query = "SELECT id, url, metadata, tags, desc FROM bookmarks \
                WHERE tags LIKE '%' || ? || '%' ORDER BY id ASC"
        logdbg('query: "%s", args: %s', query, tag)

        self.cur.execute(query, (tag,))
        results = self.cur.fetchall()
        if len(results) == 0:
            return None

        return results

    def compactdb(self, index, delay_commit=False):
        '''When an entry at index is deleted, move the
        last entry in DB to index, if index is lesser.

        :param index: DB index of deleted entry
        :param delay_commit: do not commit to DB, caller's responsibility
        '''

        self.cur.execute('SELECT MAX(id) from bookmarks')
        results = self.cur.fetchall()
        # Return if the last index left in DB was just deleted
        if len(results) == 1 and results[0][0] is None:
            return

        query1 = 'SELECT id, URL, metadata, tags, \
                 desc FROM bookmarks WHERE id = ?'
        query2 = 'DELETE FROM bookmarks WHERE id = ?'
        query3 = 'INSERT INTO bookmarks(id, URL, metadata, \
                 tags, desc) VALUES (?, ?, ?, ?, ?)'

        for row in results:
            if row[0] > index:
                self.cur.execute(query1, (row[0],))
                results = self.cur.fetchall()
                for row in results:
                    self.cur.execute(query2, (row[0],))
                    self.cur.execute(query3,
                                     (index, row[1], row[2], row[3], row[4],))
                    if not delay_commit:
                        self.conn.commit()
                    print('Index %d moved to %d' % (row[0], index))

    def delete_rec(self, index, low=0, high=0, is_range=False,
                   delay_commit=False):
        '''Delete a single record or remove the table if index is None

        :param index: DB index of deleted entry
        :param low: actual lower index of range
        :param high: actual higher index of range
        :param is_range: a range is passed using low and high arguments
        :param delay_commit: do not commit to DB, caller's responsibility
        :return: True on success, False on failure
        '''

        if is_range:  # Delete a range of indices
            # If range starts from 0, delete all records
            if low == 0:
                return self.cleardb()

            try:
                query = 'DELETE from bookmarks where id BETWEEN ? AND ?'
                self.cur.execute(query, (low, high))
                print('Index %d-%d: %d deleted'
                      % (low, high, self.cur.rowcount))
                if not self.cur.rowcount:
                    return False

                # Compact DB by ascending order of index to ensure
                # the existing higher indices move only once
                # Delayed commit is forced
                for index in range(low, high + 1):
                    self.compactdb(index, delay_commit=True)

                if not delay_commit:
                    self.conn.commit()
            except IndexError:
                logerr('No matching index')
                return False
        elif index == 0:  # Remove the table
            return self.cleardb()
        else:  # Remove a single entry
            try:
                query = 'DELETE FROM bookmarks WHERE id = ?'
                self.cur.execute(query, (index,))
                if self.cur.rowcount == 1:
                    print('Index %d deleted' % index)
                    self.compactdb(index, delay_commit=True)
                    if not delay_commit:
                        self.conn.commit()
                else:
                    logerr('No matching index %d', index)
                    return False
            except IndexError:
                logerr('No matching index %d', index)
                return False

        return True

    def delete_resultset(self, results):
        '''Delete search results in descending order of DB index.
        Indices are expected to be unique and in ascending order.
        This API forces a delayed commit.

        :param results: set of results to delete
        :return: True on success, False on failure
        '''

        resp = input('Delete the search results? (y/n): ')
        if resp != 'y':
            return False

        # delete records in reverse order
        pos = len(results) - 1
        while pos >= 0:
            idx = results[pos][0]
            self.delete_rec(idx, delay_commit=True)

            # Commit at every 200th removal
            if pos % 200 == 0:
                self.conn.commit()

            pos -= 1

        return True

    def cleardb(self):
        '''Drops the bookmark table if it exists

        :return: True on success, False on failure
        '''

        resp = input('Remove ALL bookmarks? (y/n): ')
        if resp != 'y':
            print('No bookmarks deleted')
            return False

        self.cur.execute('DROP TABLE if exists bookmarks')
        self.conn.commit()
        print('All bookmarks deleted')
        return True

    def print_rec(self, index):
        '''Print bookmark details at index or all bookmarks if index is 0
        Note: URL is printed on top because title may be blank

        :param index: index to print, 0 prints all
        '''

        if index != 0:  # Show record at index
            try:
                query = 'SELECT * FROM bookmarks WHERE id = ?'
                self.cur.execute(query, (index,))
                results = self.cur.fetchall()
                if len(results) == 0:
                    logerr('No matching index %d', index)
                    return
            except IndexError:
                logerr('No matching index %d', index)
                return

            if not self.json:
                for row in results:
                    if self.field_filter == 0:
                        print_record(row)
                    elif self.field_filter == 1:
                        print('%s\t%s' % (row[0], row[1]))
                    elif self.field_filter == 2:
                        print('%s\t%s\t%s' % (row[0], row[1], row[3][1:-1]))
                    elif self.field_filter == 3:
                        print('%s\t%s' % (row[0], row[2]))
            else:
                print(format_json(results, True, self.field_filter))
        else:  # Show all entries
            self.cur.execute('SELECT * FROM bookmarks')
            resultset = self.cur.fetchall()

            if not self.json:
                if self.field_filter == 0:
                    for row in resultset:
                        print_record(row)
                elif self.field_filter == 1:
                    for row in resultset:
                        print('%s\t%s' % (row[0], row[1]))
                elif self.field_filter == 2:
                    for row in resultset:
                        print('%s\t%s\t%s' % (row[0], row[1], row[3][1:-1]))
                elif self.field_filter == 3:
                    for row in resultset:
                        print('%s\t%s' % (row[0], row[2]))
            else:
                print(format_json(resultset, field_filter=self.field_filter))

    def get_all_tags(self):
        '''Get list of tags in DB

        :return: list of unique tags sorted alphabetically
        :return: a dictionary of {tag:usage_count}
        '''

        tags = []
        unique_tags = []
        dic = {}
        qry = 'SELECT DISTINCT tags, COUNT(tags) FROM bookmarks GROUP BY tags'
        for row in self.cur.execute(qry):
            tagset = row[0].strip(DELIM).split(DELIM)
            for tag in tagset:
                if tag not in tags:
                    dic[tag] = row[1]
                    tags += (tag,)
                else:
                    dic[tag] += row[1]

        if len(tags) == 0:
            return tags

        if tags[0] == '':
            unique_tags = sorted(tags[1:])
        else:
            unique_tags = sorted(tags)

        return unique_tags, dic

    def replace_tag(self, orig, new=None):
        '''Replace original tag by new tags in all records.
        Remove original tag if new tag is empty.

        :param orig: original tag as string
        :param new: replacement tags as list
        :return: True on success, False on failure
        '''

        newtags = DELIM

        orig = '%s%s%s' % (DELIM, orig, DELIM)
        if new is not None:
            newtags = parse_tags(new)

        if orig == newtags:
            print('Tags are same.')
            return False

        if newtags == DELIM:
            return self.delete_tag_at_index(0, orig)

        query = 'SELECT id, tags FROM bookmarks WHERE tags LIKE ?'
        self.cur.execute(query, ('%' + orig + '%',))
        results = self.cur.fetchall()

        query = 'UPDATE bookmarks SET tags = ? WHERE id = ?'
        for row in results:
            tags = row[1].replace(orig, newtags)
            tags = parse_tags([tags])
            self.cur.execute(query, (tags, row[0],))
            print('Index %d updated' % row[0])

        if len(results):
            self.conn.commit()

        return True

    def browse_by_index(self, index):
        '''Open URL at index in browser

        :param index: DB index
        :return: True on success, False on failure
        '''

        if index == 0:
            query = 'SELECT id from bookmarks ORDER BY RANDOM() LIMIT 1'
            self.cur.execute(query)
            result = self.cur.fetchone()

            # Return if no entries in DB
            if result is None:
                print('No bookmarks added yet ...')
                return False

            index = result[0]
            logdbg('Opening random index %d', index)

        query = 'SELECT URL FROM bookmarks WHERE id = ?'
        try:
            for row in self.cur.execute(query, (index,)):
                open_in_browser(row[0])
                return True
            logerr('No matching index %d', index)
        except IndexError:
            logerr('No matching index %d', index)

        return False

    def exportdb(self, filepath, markdown=False, taglist=None):
        '''Export bookmarks to a Firefox bookmarks formatted
           html or markdown file.

        :param filepath: path to file to export to
        :param markdown: use markdown syntax
        :param taglist: list of specific tags to export
        :return: True on success, False on failure
        '''

        import time

        count = 0
        timestamp = int(time.time())
        arguments = []
        query = 'SELECT * FROM bookmarks'
        is_tag_valid = False

        if taglist is not None:
            tagstr = parse_tags(taglist)

            if len(tagstr) == 0 or tagstr == DELIM:
                logerr('Invalid tag')
                return False

            if len(tagstr) > 0:
                tags = tagstr.split(DELIM)
                query = '%s WHERE' % query
                for tag in tags:
                    if tag != '':
                        is_tag_valid = True
                        query += " tags LIKE '%' || ? || '%' OR"
                        tag = '%s%s%s' % (DELIM, tag, DELIM)
                        arguments += (tag,)

                if is_tag_valid:
                    query = query[:-3]
                else:
                    query = query[:-6]

        logdbg('(%s), %s', query, arguments)
        self.cur.execute(query, arguments)
        resultset = self.cur.fetchall()

        if len(resultset) == 0:
            print('No bookmarks exported')
            return False

        if os.path.exists(filepath):
            resp = input('%s exists. Overwrite? (y/n): ' % filepath)
            if resp != 'y':
                return False

        try:
            outfp = open(filepath, mode='w', encoding='utf-8')
        except Exception as e:
            logerr(e)
            return False

        if not markdown:
            outfp.write('''<!DOCTYPE NETSCAPE-Bookmark-file-1>

<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>

<DL><p>
    <DT><H3 ADD_DATE="%s" LAST_MODIFIED="%s" PERSONAL_TOOLBAR_FOLDER="true">\
Buku bookmarks</H3>
    <DL><p>
''' % (timestamp, timestamp))

            for row in resultset:
                out = '%s<DT><A HREF="%s" ADD_DATE="%s" LAST_MODIFIED="%s"' \
                        % ('        ', row[1], timestamp, timestamp)
                if row[3] != DELIM:
                    out = '%s TAGS="%s"' % (out, row[3][1:-1])
                out = '%s>%s</A>\n' % (out, row[2])
                if row[4] != '':
                    out = '%s        <DD>%s\n' % (out, row[4])

                outfp.write(out)
                count += 1

            outfp.write('    </DL><p>\n</DL><p>')
        else:
            outfp.write('List of buku bookmarks:\n\n')
            for row in resultset:
                if row[2] == '':
                    out = '- [Untitled](%s)\n' % (row[1])
                else:
                    out = '- [%s](%s)\n' % (row[2], row[1])
                outfp.write(out)
                count += 1

        outfp.close()
        print('%s exported' % count)
        return True

    def importdb(self, filepath, markdown=False):
        '''Import bookmarks from a html or markdown file.
        Supports Firefox, Google Chrome and IE exported html

        :param filepath: path to file to import
        :param markdown: use markdown syntax
        :return: True on success, False on failure
        '''

        if not markdown:
            try:
                import bs4
                with open(filepath, mode='r', encoding='utf-8') as infp:
                    soup = bs4.BeautifulSoup(infp, 'html.parser')
            except ImportError:
                logerr('Beautiful Soup not found')
                return False
            except Exception as e:
                logerr(e)
                return False

            html_tags = soup.findAll('a')
            for tag in html_tags:
                # Extract comment from <dd> tag
                desc = None
                comment_tag = tag.findNextSibling('dd')
                if comment_tag:
                    desc = comment_tag.text[0:comment_tag.text.find('\n')]

                self.add_rec(tag['href'], tag.string, ('%s%s%s' %
                             (DELIM, tag['tags'], DELIM))
                             if tag.has_attr('tags') else None,
                             desc, 0, True)

            self.conn.commit()
            infp.close()
        else:
            with open(filepath, mode='r', encoding='utf-8') as infp:
                for line in infp:
                    # Supported markdown format: [title](url)
                    # Find position of title end, url start delimiter combo
                    index = line.find('](')
                    if index != -1:
                        # Find title start delimiter
                        title_start_delim = line[:index].find('[')
                        # Reverse find the url end delimiter
                        url_end_delim = line[index + 2:].rfind(')')

                        if title_start_delim != -1 and url_end_delim > 0:
                            # Parse title
                            title = line[title_start_delim + 1:index]
                            # Parse url
                            url = line[index + 2:index + 2 + url_end_delim]

                            self.add_rec(url, title, None, None, 0, True)

            self.conn.commit()
            infp.close()

        return True

    def mergedb(self, path):
        '''Merge bookmarks from another Buku database file

        :param path: path to DB file to merge
        :return: True on success, False on failure
        '''

        try:
            # Connect to input DB
            if sys.version_info >= (3, 4, 4):
                # Python 3.4.4 and above
                indb_conn = sqlite3.connect('file:%s?mode=ro' % path, uri=True)
            else:
                indb_conn = sqlite3.connect(path)

            indb_cur = indb_conn.cursor()
            indb_cur.execute('SELECT * FROM bookmarks')
        except Exception as e:
            logerr(e)
            return False

        resultset = indb_cur.fetchall()
        for row in resultset:
            self.add_rec(row[1], row[2], row[3], row[4], row[5], True)

        if len(resultset):
            self.conn.commit()

        try:
            indb_cur.close()
            indb_conn.close()
        except Exception:
            pass

        return True

    def tnyfy_url(self, index=0, url=None, shorten=True):
        '''Shorted a URL using Google URL shortener

        :param index: shorten the URL at DB index (int)
        :param url: pass a URL (string)
        :param shorten: True (default) to shorten, False to expand (boolean)
        :return: shortened url string on success, None on failure
        '''

        if not index and not url:
            logerr('Either a valid DB index or URL required')
            return None

        if index:
            self.cur.execute('SELECT url FROM bookmarks WHERE id = ?',
                             (index,))
            results = self.cur.fetchall()
            if len(results):
                url = results[0][0]
            else:
                return None

        proxies = {
            'https': os.environ.get('https_proxy'),
        }

        urlbase = 'https://tny.im/yourls-api.php?action='
        if shorten:
            from urllib.parse import quote_plus as qp

            _u = '%s%s%s' % (urlbase, 'shorturl&format=simple&url=', qp(url))
        else:
            _u = '%s%s%s' % (urlbase, 'expand&format=simple&shorturl=', url)

        try:
            r = requests.post(_u,
                              headers={
                                       'content-type': 'application/json',
                                       'User-Agent': USER_AGENT
                                      },
                              proxies=proxies)
        except Exception as e:
            logerr(e)
            return None

        if r.status_code != 200:
            logerr('[%s] %s', r.status_code, r.reason)
            return None

        return r.text

    def fixtags(self):
        '''Undocumented API to fix tags set
        in earlier versions. Functionalities:

        1. Remove duplicate tags
        2. Sort tags
        3. Use lower case to store tags
        '''

        to_commit = False
        self.cur.execute('SELECT id, tags FROM bookmarks ORDER BY id ASC')
        resultset = self.cur.fetchall()
        query = 'UPDATE bookmarks SET tags = ? WHERE id = ?'
        for row in resultset:
            oldtags = row[1]
            if oldtags == ',':
                continue

            tags = parse_tags([oldtags])
            if tags == oldtags:
                continue

            self.cur.execute(query, (tags, row[0],))
            to_commit = True

        if to_commit:
            self.conn.commit()

    def close_quit(self, exitval=0):
        '''Close a DB connection and exit

        :param exitval: program exit value
        '''

        if self.conn is not None:
            try:
                self.cur.close()
                self.conn.close()
            except Exception:
                # ignore errors here, we're closing down
                pass
        sys.exit(exitval)


class ExtendedArgumentParser(argparse.ArgumentParser):
    '''Extend classic argument parser'''

    # Print program info
    @staticmethod
    def print_program_info(file=sys.stdout):
        if sys.platform == 'win32' and file == sys.stdout:
            file = sys.stderr

        file.write('''
SYMBOLS:
      >                    title
      +                    comment
      #                    tags

Version %s
Copyright © 2015-2017 %s
License: %s
Webpage: https://github.com/jarun/Buku
''' % (__version__, __author__, __license__))

    # Print prompt help
    @staticmethod
    def print_prompt_help(file=sys.stdout):
        file.write('''
keys:
  1-N                  browse search result indices and/or ranges
  a                    open all results in browser
  s keyword [...]      search for records with ANY keyword
  S keyword [...]      search for records with ALL keywords
  d                    match substrings ('pen' matches 'opened')
  r expression         run a regex search
  t [...]              search bookmarks by a tag or show tag list
                       (tag list index fetches bookmarks by tag)
  ?                    show this help
  q, ^D, double Enter  exit buku

''')

    # Help
    def print_help(self, file=sys.stdout):
        super(ExtendedArgumentParser, self).print_help(file)
        self.print_program_info(file)


# ----------------
# Helper functions
# ----------------

def is_bad_url(url):
    '''Check if URL is malformed
    This API is not bulletproof but works in most cases.

    :param url: URL to scan
    :return: True or False
    '''

    # Get the netloc token
    netloc = parse_url(url).netloc
    if not netloc:
        # Try of prepend '//' and get netloc
        netloc = parse_url('//' + url).netloc
        if not netloc:
            return True

    logdbg('netloc: %s', netloc)

    # netloc cannot start or end with a '.'
    if netloc.startswith('.') or netloc.endswith('.'):
        return True

    # netloc should have at least one '.'
    index = netloc.rfind('.')
    if index < 0:
        return True

    return False


def is_ignored_mime(url):
    '''Check if URL links to ignored mime
    Only a 'HEAD' request is made for these URLs

    :param url: URL to scan
    :return: True or False
    '''

    for mime in SKIP_MIMES:
        if url.lower().endswith(mime):
            return True

    return False


def get_page_title(resp):
    '''Invoke HTML parser and extract title from HTTP response

    :param resp: HTTP(S) GET response
    :return: title fetched from parsed page
    '''

    parser = BukuHTMLParser()

    try:
        parser.feed(resp.data.decode(errors='replace'))
    except Exception as e:
        # Suppress Exception due to intentional self.reset() in BHTMLParser
        if logger.isEnabledFor(logging.DEBUG) \
                and str(e) != 'we should not get here!':
            logerr('get_page_title(): %s', e)
    finally:
        return parser.parsed_title


def gen_headers():
    '''Generate headers for network connection'''

    global myheaders, myproxy

    myheaders = {
                 'Accept-Encoding': 'gzip,deflate',
                 'User-Agent': USER_AGENT,
                 'Accept': '*/*',
                 'Cookie': '',
                 'DNT': '1'
                }

    myproxy = os.environ.get('https_proxy')
    if myproxy:
        try:
            url = parse_url(myproxy)
        except Exception as e:
            logerr(e)
            return

        # Strip username and password (if present) and update headers
        if url.auth:
            myproxy = myproxy.replace(url.auth + '@', '')
            auth_headers = make_headers(basic_auth=url.auth)
            myheaders.update(auth_headers)

        logdbg('proxy: [%s]', myproxy)


def get_PoolManager():
    '''Creates a pool manager with proxy support, if applicable

    :return: ProxyManager if https_proxy is defined, else PoolManager.
    '''

    if myproxy:
        return urllib3.ProxyManager(myproxy, num_pools=1, headers=myheaders)

    return urllib3.PoolManager(num_pools=1, headers=myheaders)


def network_handler(url):
    '''Handle server connection and redirections

    :param url: URL to fetch
    :return: (title, recognized mime, bad url) tuple
    '''

    http_handler = None
    page_title = None
    resp = None
    method = 'GET'

    if is_bad_url(url):
        return ('', 0, 1)

    if is_ignored_mime(url):
        method = 'HEAD'

    if not myheaders:
        gen_headers()

    try:
        http_handler = get_PoolManager()

        while True:
            resp = http_handler.request(method, url, timeout=40)

            if resp.status == 200:
                if method == 'GET':
                    page_title = get_page_title(resp)
            elif resp.status == 403 and url.endswith('/'):
                # HTTP response Forbidden
                # Handle URLs in the form of https://www.domain.com/
                # which fail when trying to fetch resource '/'
                # retry without trailing '/'

                logdbg('Received status 403: retrying...')
                # Remove trailing /
                url = url[:-1]
                resp.release_conn()
                continue
            else:
                logerr('[%s] %s', resp.status, resp.reason)

            if resp:
                resp.release_conn()

            break
    except Exception as e:
        logerr('network_handler(): %s', e)
    finally:
        if http_handler:
            http_handler.clear()
        if method == 'HEAD':
            return ('', 1, 0)
        if page_title is None:
            return ('', 0, 0)
        return (page_title.strip().replace('\n', ''), 0, 0)


def parse_tags(keywords=None):
    '''Format and get tag string from tokens

    :param keywords: list of tags
    :return: comma-delimited string of tags
    :return: just delimiter, if no keywords
    :return: None, if keyword is None
    '''

    if keywords is None:
        return None

    tags = DELIM
    orig_tags = []
    unique_tags = []

    # Cleanse and get the tags
    tagstr = ' '.join(keywords)
    marker = tagstr.find(DELIM)

    while marker >= 0:
        token = tagstr[0:marker]
        tagstr = tagstr[marker + 1:]
        marker = tagstr.find(DELIM)
        token = token.strip()
        if token == '':
            continue

        tags = '%s%s%s' % (tags, token, DELIM)

    tagstr = tagstr.strip()
    if tagstr != '':
        tags = '%s%s%s' % (tags, tagstr, DELIM)

    logdbg('keywords: %s', keywords)
    logdbg('parsed tags: [%s]', tags)

    if tags == DELIM:
        return tags

    orig_tags += tags.strip(DELIM).split(DELIM)
    for tag in orig_tags:
        if tag.lower() not in unique_tags:
            # Add unique tags in lower case
            unique_tags += (tag.lower(), )

    # Sort the tags
    sorted_tags = sorted(unique_tags)

    # Wrap with delimiter
    return '%s%s%s' % (DELIM, DELIM.join(sorted_tags), DELIM)


def taglist_subprompt(obj, msg, noninteractive=False):
    '''Additional prompt to show unique tag list

    :param obj: a valid instance of BukuDb class
    :param msg: sub-prompt message
    :param noninteractive: do not seek user input
    :return: new command string
    '''

    unique_tags, dic = obj.get_all_tags()
    new_results = True

    while True:
        if new_results:
            if len(unique_tags) == 0:
                count = 0
                print('0 tags')
            else:
                count = 1
                for tag in unique_tags:
                    print('%6d. %s (%d)' % (count, tag, dic[tag]))
                    count += 1
                print()

            if noninteractive:
                return

        try:
            nav = input(msg)
            if not nav:
                nav = input(msg)
                if not nav:
                    # Quit on double enter
                    return 'q'
            nav = nav.strip()
        except EOFError:
            return 'q'

        if is_int(nav) and int(nav) > 0 and int(nav) < count:
            return 't ' + unique_tags[int(nav) - 1]
        elif is_int(nav):
            print('No matching index %s' % nav)
            new_results = False
        elif is_int(nav[0]):
            print('Invalid input')
            new_results = False
        elif nav == 't':
            new_results = True
            continue
        elif (nav == 'q' or nav == 'd' or nav == '?' or
              nav.startswith('s ') or nav.startswith('S ') or
              nav.startswith('r ') or nav.startswith('t ')):
            return nav
        else:
            print('Invalid input')
            new_results = False


def prompt(obj, results, noninteractive=False, deep=False, subprompt=False):
    '''Show each matching result from a search and prompt

    :param obj: a valid instance of BukuDb class
    :param results: result set from a DB query
    :param noninteractive: do not seek user input
    :param deep: use deep search
    :param subprompt: jump directly to sub prompt
    '''

    if not type(obj) is BukuDb:
        logerr('Not a BukuDb instance')
        return

    new_results = True
    if colorize:
        msg = '\x1b[7mbuku (? for help)\x1b[0m '
    else:
        msg = 'buku (? for help): '

    while True:
        if not subprompt:
            if new_results:
                if results:
                    count = 0

                    for row in results:
                        count += 1
                        print_record(row, count)
                else:
                    print('0 results')

                if noninteractive:
                    return

            try:
                nav = input(msg)
                if not nav:
                    nav = input(msg)
                    if not nav:
                        # Quit on double enter
                        break
                nav = nav.strip()
            except EOFError:
                return
        else:
            nav = 't'
            subprompt = False

        # list tags with 't'
        if nav == 't':
            nav = taglist_subprompt(obj, msg, noninteractive)
            if noninteractive:
                return

        # search ANY match with new keywords
        if nav.startswith('s '):
            results = obj.searchdb(nav[2:].split(), False, deep)
            new_results = True
            continue

        # search ALL match with new keywords
        if nav.startswith('S '):
            results = obj.searchdb(nav[2:].split(), True, deep)
            new_results = True
            continue

        # regular expressions search with new keywords
        if nav.startswith('r '):
            results = obj.searchdb(nav[2:].split(), True, regex=True)
            new_results = True
            continue

        # tag search with new keywords
        if nav.startswith('t '):
            results = obj.search_by_tag(nav[2:])
            new_results = True
            continue

        # quit with 'q'
        if nav == 'q':
            return

        # toggle deep search with 'd'
        if nav == 'd':
            deep = not deep
            if deep:
                print('deep search on')
            else:
                print('deep search off')

            new_results = False
            continue

        # Show help with '?'
        if nav == '?':
            ExtendedArgumentParser.print_prompt_help(sys.stdout)
            new_results = False
            continue

        new_results = False

        # Nothing to browse if there are no results
        if not results:
            print('Not in a search context')
            continue

        # open all results and re-prompt with 'a'
        if nav == 'a':
            for index in range(0, count):
                try:
                    open_in_browser(results[index][1])
                except Exception as e:
                    logerr('prompt() 1: %s', e)

            continue

        # iterate over white-space separated indices
        for nav in nav.split():
            if is_int(nav):
                index = int(nav) - 1
                if index < 0 or index >= count:
                    print('No matching index %s' % nav)
                    continue
                try:
                    open_in_browser(results[index][1])
                except Exception as e:
                    logerr('prompt() 2: %s', e)
            elif '-' in nav and is_int(nav.split('-')[0]) \
                    and is_int(nav.split('-')[1]):
                lower = int(nav.split('-')[0])
                upper = int(nav.split('-')[1])
                if lower > upper:
                    lower, upper = upper, lower
                for index in range(lower-1, upper):
                    try:
                        if 0 <= index < count:
                            open_in_browser(results[index][1])
                        else:
                            print('No matching index %d' % (index + 1))
                    except Exception as e:
                        logerr('prompt() 3: %s', e)
            else:
                print('Invalid input')
                break


def print_record(row, idx=0):
    '''Print a single DB record
    Handles both search result and individual record

    :param idx: search result index. If 0, print with DB index
    '''

    # Start with index and URL
    if idx != 0:
        pr = ID_str % (idx, row[1], row[0])
    else:
        pr = ID_DB_str % (row[0], row[1])
        # Indicate if record is immutable
        if row[5] & 1:
            pr = MUTE_str % (pr)
        else:
            pr = '%s\n' % (pr)

    # Append title
    if row[2] != '':
        pr = TITLE_str % (pr, row[2])

    # Append description
    if row[4] != '':
        pr = DESC_str % (pr, row[4])

    # Append tags IF not default (delimiter)
    if row[3] != DELIM:
        pr = TAG_str % (pr, row[3][1:-1])

    print(pr)


def format_json(resultset, single_record=False, field_filter=0):
    '''Return results in Json format

    :param single_record: indicates only one record
    :param field_filter: determines fields to show
    :return: record(s) in Json format
    '''

    if single_record:
        marks = {}
        for row in resultset:
            if field_filter == 1:
                marks['uri'] = row[1]
            elif field_filter == 2:
                marks['uri'] = row[1]
                marks['tags'] = row[3][1:-1]
            elif field_filter == 3:
                marks['title'] = row[2]
            else:
                marks['index'] = row[0]
                marks['uri'] = row[1]
                marks['title'] = row[2]
                marks['description'] = row[4]
                marks['tags'] = row[3][1:-1]
    else:
        marks = []
        for row in resultset:
            if field_filter == 1:
                record = {'uri': row[1]}
            elif field_filter == 2:
                record = {'uri': row[1], 'tags': row[3][1:-1]}
            elif field_filter == 3:
                record = {'title': row[2]}
            else:
                record = {'index': row[0], 'uri': row[1], 'title': row[2],
                          'description': row[4], 'tags': row[3][1:-1]}

            marks.append(record)

    return json.dumps(marks, sort_keys=True, indent=4)


def is_int(string):
    '''Check if a string is a digit

    :param string: input string
    :return: True on success, False on exception
    '''

    try:
        int(string)
        return True
    except Exception:
        return False


def open_in_browser(url):
    '''Duplicate stdin, stdout (to suppress showing errors
    on the terminal) and open URL in default browser

    :param url: URL to open
    '''

    if not parse_url(url).scheme:
        # Prefix with 'http://' is no scheme
        # Otherwise, opening in browser fails anyway
        # We expect http to https redirection
        # will happen for https-only websites
        logerr('scheme missing in URI, trying http')
        url = '%s%s' % ('http://', url)

    _stderr = os.dup(2)
    os.close(2)
    _stdout = os.dup(1)
    os.close(1)
    fd = os.open(os.devnull, os.O_RDWR)
    os.dup2(fd, 2)
    os.dup2(fd, 1)
    try:
        webbrowser.open(url)
    except Exception as e:
        logerr('open_in_browser(): %s', e)
    finally:
        os.close(fd)
        os.dup2(_stderr, 2)
        os.dup2(_stdout, 1)


def check_upstream_release():
    '''Check and report the latest upstream release version'''

    proxies = {
        'https': os.environ.get('https_proxy'),
    }

    try:
        r = requests.get(
                    'https://api.github.com/repos/jarun/buku/tags?per_page=1',
                    proxies=proxies
                        )
    except Exception as e:
        logerr(e)
        return

    if r.status_code != 200:
        logerr('[%s] %s', r.status_code, r.reason)
    else:
        latest = r.json()[0]['name']
        if latest == 'v' + __version__:
            print('This is the latest release')
        else:
            print('Latest upstream release is %s' % latest)


def regexp(expr, item):
    '''Perform a regular expression search'''

    return re.search(expr, item, re.IGNORECASE) is not None


def sigint_handler(signum, frame):
    '''Custom SIGINT handler'''

    global interrupted

    interrupted = True
    print('\nInterrupted.', file=sys.stderr)

    # Do a hard exit from here
    os._exit(1)

signal.signal(signal.SIGINT, sigint_handler)


# Handle piped input
def piped_input(argv, pipeargs=None):
    if not sys.stdin.isatty():
        pipeargs.extend(argv)
        for s in sys.stdin.readlines():
            pipeargs.extend(s.split())


# main starts here
def main():
    global colorize, ID_str, ID_DB_str, MUTE_str, TITLE_str, DESC_str, TAG_str

    title_in = None
    tags_in = None
    desc_in = None
    pipeargs = []

    try:
        piped_input(sys.argv, pipeargs)
    except KeyboardInterrupt:
        pass

    # If piped input, set argument vector
    if len(pipeargs) > 0:
        sys.argv = pipeargs

    # Setup custom argument parser
    argparser = ExtendedArgumentParser(
        description='Powerful command-line bookmark manager. Your mini web!',
        formatter_class=argparse.RawTextHelpFormatter,
        usage='''buku [OPTIONS] [KEYWORD [KEYWORD ...]]''',
        add_help=False
    )
    HIDE = argparse.SUPPRESS

    # ---------------------
    # GENERAL OPTIONS GROUP
    # ---------------------

    general_grp = argparser.add_argument_group(
        title='GENERAL OPTIONS',
        description='''    -a, --add URL [tag, ...]
                         bookmark URL with comma-separated tags
    -u, --update [...]   update fields of bookmark at DB indices
                         accepts indices and ranges
                         refresh all titles, if no arguments
                         refresh titles of bookmarks at indices,
                         if no edit options are specified
                         update search results when used with
                         search options, if no arguments
    -d, --delete [...]   delete bookmarks. Valid inputs: either
                         a hyphenated single range (100-200),
                         OR space-separated indices (100 15 200)
                         delete all bookmarks, if no arguments
                         delete search results when used with
                         search options, if no arguments
    -v, --version        show program version and exit
    -h, --help           show this information and exit''')
    addarg = general_grp.add_argument
    addarg('-a', '--add', nargs='+', help=HIDE)
    addarg('-u', '--update', nargs='*', help=HIDE)
    addarg('-d', '--delete', nargs='*', help=HIDE)
    addarg('-v', '--version', action='version', version=__version__, help=HIDE)
    addarg('-h', '--help', action='store_true', help=HIDE)

    # ------------------
    # EDIT OPTIONS GROUP
    # ------------------

    edit_grp = argparser.add_argument_group(
        title='EDIT OPTIONS',
        description='''    --url keyword        specify url, works only with -u option
    --tag [+|-] [...]    set comma-separated tags with -a and -u
                         clear tags, if no arguments
                         works with -a, -u
                         append to tags, if preceded by '+'
                         remove from tags, if preceded by '-'
    -t, --title [...]    manually set title, works with -a, -u
                         if no arguments:
                         -a: do not set title, -u: clear title
    -c, --comment [...]  description of the bookmark, works with
                         -a, -u; clears comment, if no arguments
    --immutable N        disable title fetch from web on update
                         works with -a, -u
                         N=0: mutable (default), N=1: immutable''')
    addarg = edit_grp.add_argument
    addarg('--url', nargs=1, help=HIDE)
    addarg('--tag', nargs='*', help=HIDE)
    addarg('-t', '--title', nargs='*', help=HIDE)
    addarg('-c', '--comment', nargs='*', help=HIDE)
    addarg('--immutable', type=int, default=-1, choices={0, 1}, help=HIDE)

    # --------------------
    # SEARCH OPTIONS GROUP
    # --------------------

    search_grp = argparser.add_argument_group(
        title='SEARCH OPTIONS',
        description='''    -s, --sany keyword [...]
                         find records with ANY search keyword
    -S, --sall keyword [...]
                         find records with ALL search keywords
                         special keywords -
                         "blank": entries with empty title/tag
                         "immutable": entries with locked title
    --deep               match substrings ('pen' matches 'opens')
    --sreg expression    run a regex search
    --stag [...]         search bookmarks by a tag
                         list all tags, if no arguments''')
    addarg = search_grp.add_argument
    addarg('-s', '--sany', nargs='+', help=HIDE)
    addarg('-S', '--sall', nargs='+', help=HIDE)
    addarg('--sreg', nargs=1, help=HIDE)
    addarg('--deep', action='store_true', help=HIDE)
    addarg('--stag', nargs='*', help=HIDE)

    # ------------------------
    # ENCRYPTION OPTIONS GROUP
    # ------------------------

    crypto_grp = argparser.add_argument_group(
        title='ENCRYPTION OPTIONS',
        description='''    -l, --lock [N]       encrypt DB file with N (> 0, default 8)
                         hash iterations to generate key
    -k, --unlock [N]     decrypt DB file with N (> 0, default 8)
                         hash iterations to generate key''')
    addarg = crypto_grp.add_argument
    addarg('-k', '--unlock', nargs='?', type=int, const=8, help=HIDE)
    addarg('-l', '--lock', nargs='?', type=int, const=8, help=HIDE)

    # ----------------
    # POWER TOYS GROUP
    # ----------------

    power_grp = argparser.add_argument_group(
        title='POWER TOYS',
        description='''    -e, --export file    export bookmarks to Firefox format html
                         use --tag to export only specific tags
    -i, --import file    import bookmarks from html file
                         FF and Google Chrome formats supported
    --markdown           use markdown with -e and -i
                         format: [title](url), 1 per line
    -m, --merge file     add bookmarks from another buku DB file
    -p, --print [...]    show details of bookmark by DB index
                         accepts indices and ranges
                         show all bookmarks, if no arguments
    -f, --format N       limit fields in -p or Json search output
                         1: URL, 2: URL and tag, 3: title
    -r, --replace oldtag [newtag ...]
                         replace oldtag with newtag everywhere
                         delete oldtag, if no newtag
    -j, --json           Json formatted output for -p and search
    --nocolor            disable color output
    --noprompt           do not show the prompt, run and exit
    -o, --open [...]     open bookmarks in browser by DB index
                         accepts indices and ranges
                         open a random index, if no arguments
    --shorten N/URL      fetch shortened url from tny.im service
                         accepts either a DB index or a URL
    --expand N/URL       expand a tny.im shortened url
    --tacit              reduce verbosity
    --threads N          max network connections in full refresh
                         default 4, min 1, max 10
    --upstream           check latest upstream version available
    -z, --debug          show debug information and verbose logs''')
    addarg = power_grp.add_argument
    addarg('-e', '--export', nargs=1, help=HIDE)
    addarg('-i', '--import', nargs=1, dest='importfile', help=HIDE)
    addarg('--markdown', action='store_true', help=HIDE)
    addarg('-m', '--merge', nargs=1, help=HIDE)
    addarg('-p', '--print', nargs='*', help=HIDE)
    addarg('-f', '--format', type=int, default=0, choices={1, 2, 3}, help=HIDE)
    addarg('-r', '--replace', nargs='+', help=HIDE)
    addarg('-j', '--json', action='store_true', help=HIDE)
    addarg('--nocolor', action='store_true', help=HIDE)
    addarg('--noprompt', action='store_true', help=HIDE)
    addarg('-o', '--open', nargs='*', help=HIDE)
    addarg('--shorten', nargs=1, help=HIDE)
    addarg('--expand', nargs=1, help=HIDE)
    addarg('--tacit', action='store_true', help=HIDE)
    addarg('--threads', type=int, default=4, choices=range(1, 11), help=HIDE)
    addarg('--upstream', action='store_true', help=HIDE)
    addarg('-z', '--debug', action='store_true', help=HIDE)
    # Undocumented API
    addarg('--fixtags', action='store_true', help=HIDE)

    # Show help and exit if no arguments
    if len(sys.argv) < 2:
        argparser.print_help(sys.stdout)
        sys.exit(1)

    # Parse the arguments
    args = argparser.parse_args()

    # Show help and exit if help requested
    if args.help:
        argparser.print_help(sys.stdout)
        sys.exit(0)

    # Set up debugging
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logdbg('Version %s', __version__)
    else:
        logging.disable(logging.WARNING)
        urllib3.disable_warnings()

    # Handle color output preference
    if args.nocolor:
        colorize = False
        ID_str = '%d. %s [%s]\n'
        ID_DB_str = '%d. %s'
        MUTE_str = '%s (L)\n'
        TITLE_str = '%s   > %s\n'
        DESC_str = '%s   + %s\n'
        TAG_str = '%s   # %s\n'

    # Handle encrypt/decrypt options at top priority
    if args.lock is not None:
        BukuCrypt.encrypt_file(args.lock)

    if args.unlock is not None:
        BukuCrypt.decrypt_file(args.unlock)

    # Set up tags
    if args.tag is not None:
        if len(args.tag):
            tags_in = args.tag
        else:
            tags_in = [DELIM, ]

    # Set up title
    if args.title is not None:
        if len(args.title):
            title_in = ' '.join(args.title)
        else:
            title_in = ''

    # Set up comment
    if args.comment is not None:
        if len(args.comment) > 0:
            desc_in = ' '.join(args.comment)
        else:
            desc_in = ''

    # Initialize the database and get handles, set verbose by default
    bdb = BukuDb(args.json, args.format, not args.tacit,
                 colorize=not args.nocolor)

    # Add record
    if args.add is not None:
        # Parse tags into a comma-separated string
        tags = DELIM
        keywords = args.add
        if tags_in is not None:
            if tags_in[0] == '+' and len(tags_in) == 1:
                pass
            elif tags_in[0] == '+':
                # The case: buku -a url tag1, tag2 --tag + tag3, tag4
                tags_in = tags_in[1:]
                # In case of add, args.add may have URL followed by tags
                # Add delimiter as url+tags may not end with one
                keywords = args.add + [DELIM] + tags_in
            else:
                keywords = args.add + [DELIM] + tags_in

        if len(keywords) > 1:
            tags = parse_tags(keywords[1:])

        bdb.add_rec(args.add[0], title_in, tags, desc_in, args.immutable)

    # Search record
    search_results = None
    search_opted = True
    update_search_results = False

    if args.sany is not None:
        # Search URLs, titles, tags for any keyword
        search_results = bdb.searchdb(args.sany, False, args.deep)
    elif args.sall is not None:
        # Search URLs, titles, tags with all keywords
        search_results = bdb.searchdb(args.sall, True, args.deep)
    elif args.sreg is not None:
        # Run a regular expression search
        search_results = bdb.searchdb(args.sreg, regex=True)
    elif args.stag is not None:
        # Search bookmarks by tag
        if len(args.stag):
            search_results = bdb.search_by_tag(' '.join(args.stag))
        else:
            # Use sub prompt to list all tags
            prompt(bdb, None, args.noprompt, subprompt=True)
    else:
        search_opted = False

    if search_results:
        oneshot = args.noprompt
        to_delete = False

        # In case of search and delete/update,
        # prompt should be non-interactive
        # delete gets priority over update
        if args.delete is not None and len(args.delete) == 0:
            oneshot = True
            to_delete = True
        elif args.update is not None and len(args.update) == 0:
            oneshot = True
            update_search_results = True

        if not args.json:
            prompt(bdb, search_results, oneshot, args.deep)
        else:
            # Printing in Json format is non-interactive
            print(format_json(search_results, field_filter=args.format))

        # Delete search results if opted
        if to_delete:
            bdb.delete_resultset(search_results)

    # Update record
    if args.update is not None:
        if args.url is not None:
            url_in = args.url[0]
        else:
            url_in = ''

        # Parse tags into a comma-separated string
        if tags_in and len(tags_in):
            if tags_in[0] == '+':
                tags = '+%s' % parse_tags(tags_in[1:])
            elif tags_in[0] == '-':
                tags = '-%s' % parse_tags(tags_in[1:])
            else:
                tags = parse_tags(tags_in)
        else:
            tags = None

        if len(args.update) == 0:
            # Update all records only if search was not opted
            if not search_opted:
                bdb.update_rec(0, url_in, title_in, tags, desc_in,
                               args.immutable, args.threads)
            elif update_search_results and search_results is not None:
                print("Updated results:\n")

                pos = len(search_results) - 1
                while pos >= 0:
                    idx = search_results[pos][0]
                    bdb.update_rec(idx, url_in, title_in, tags, desc_in,
                                   args.immutable, args.threads)

                    # Commit at every 200th removal
                    if pos % 200 == 0:
                        bdb.conn.commit()

                    pos -= 1
        else:
            for idx in args.update:
                if is_int(idx):
                    bdb.update_rec(int(idx), url_in, title_in, tags, desc_in,
                                   args.immutable, args.threads)
                elif '-' in idx and is_int(idx.split('-')[0]) \
                        and is_int(idx.split('-')[1]):
                    lower = int(idx.split('-')[0])
                    upper = int(idx.split('-')[1])
                    if lower > upper:
                        lower, upper = upper, lower

                    # Update only once if range starts from 0 (all)
                    if lower == 0:
                        bdb.update_rec(0, url_in, title_in, tags, desc_in,
                                       args.immutable, args.threads)
                    else:
                        for _id in range(lower, upper + 1):
                            bdb.update_rec(_id, url_in, title_in, tags,
                                           desc_in, args.immutable,
                                           args.threads)
                            if interrupted:
                                break

                if interrupted:
                    break

    # Delete record
    if args.delete is not None:
        if len(args.delete) == 0:
            # Attempt delete-all only if search was not opted
            if not search_opted:
                bdb.cleardb()
        elif len(args.delete) == 1 and '-' in args.delete[0]:
            vals = str(args.delete[0]).split('-')
            if len(vals) == 2 and is_int(vals[0]) and is_int(vals[1]):
                if int(vals[0]) == int(vals[1]):
                    bdb.delete_rec(int(vals[0]))
                elif int(vals[0]) < int(vals[1]):
                    bdb.delete_rec(0, int(vals[0]), int(vals[1]), True)
                else:
                    bdb.delete_rec(0, int(vals[1]), int(vals[0]), True)
            else:
                logerr('Invalid index or range')
                bdb.close_quit(1)
        else:
            ids = []
            # Select the unique indices
            for idx in args.delete:
                if idx not in ids:
                    ids += (idx,)

            try:
                # Index delete order - highest to lowest
                ids.sort(key=lambda x: int(x), reverse=True)
                for idx in ids:
                    bdb.delete_rec(int(idx))
            except ValueError:
                logerr('Invalid index or range')

    # Print record
    if args.print is not None:
        if len(args.print) == 0:
            bdb.print_rec(0)
        else:
            for idx in args.print:
                if is_int(idx):
                    bdb.print_rec(int(idx))
                elif '-' in idx and is_int(idx.split('-')[0]) \
                        and is_int(idx.split('-')[1]):
                    lower = int(idx.split('-')[0])
                    upper = int(idx.split('-')[1])
                    if lower > upper:
                        lower, upper = upper, lower
                    for _id in range(lower, upper + 1):
                        bdb.print_rec(_id)
                else:
                    logerr('Invalid index or range to print')
                    bdb.close_quit(1)

    # Replace a tag in DB
    if args.replace is not None:
        if len(args.replace) == 1:
            bdb.delete_tag_at_index(0, args.replace[0])
        else:
            bdb.replace_tag(args.replace[0], args.replace[1:])

    # Export bookmarks
    if args.export is not None:
        if args.tag is None:
            bdb.exportdb(args.export[0], args.markdown)
        elif len(args.tag) == 0:
            logerr('Missing tag')
        else:
            bdb.exportdb(args.export[0], args.markdown, args.tag)

    # Import bookmarks
    if args.importfile is not None:
        bdb.importdb(args.importfile[0], args.markdown)

    # Merge a database file and exit
    if args.merge is not None:
        bdb.mergedb(args.merge[0])

    # Open URL in browser
    if args.open is not None:
        if len(args.open) == 0:
            bdb.browse_by_index(0)
        else:
            for idx in args.open:
                if is_int(idx):
                    bdb.browse_by_index(int(idx))
                elif '-' in idx and is_int(idx.split('-')[0]) \
                        and is_int(idx.split('-')[1]):
                    lower = int(idx.split('-')[0])
                    upper = int(idx.split('-')[1])
                    if lower > upper:
                        lower, upper = upper, lower
                    for _id in range(lower, upper + 1):
                        bdb.browse_by_index(_id)
                else:
                    logerr('Invalid index or range to open')
                    bdb.close_quit(1)

    # Shorten URL
    if args.shorten:
        if is_int(args.shorten[0]):
            shorturl = bdb.tnyfy_url(index=int(args.shorten[0]))
        else:
            shorturl = bdb.tnyfy_url(url=args.shorten[0])

        if shorturl:
            print(shorturl)

    # Expand URL
    if args.expand:
        if is_int(args.expand[0]):
            url = bdb.tnyfy_url(index=int(args.expand[0]), shorten=False)
        else:
            url = bdb.tnyfy_url(url=args.expand[0], shorten=False)

        if url:
            print(url)

    # Report upstream version
    if args.upstream:
        check_upstream_release()

    # Fix tags
    if args.fixtags:
        bdb.fixtags()

    # Close DB connection and quit
    bdb.close_quit(0)

if __name__ == '__main__':
    main()
