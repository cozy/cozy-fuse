#!/usr/bin/env python

# -*- coding: utf-8 -*-
#
# Copyright (C) 2008 Jason Davies
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.

import os
import platform
import errno
import fuse
import stat
import subprocess
import logging
import datetime
import calendar
import cache

import dbutils
import binarycache
import local_config

ATTR_VALIDITY_PERIOD = datetime.timedelta(seconds=10)

DEVNULL = open(os.devnull, 'wb')

fuse.fuse_python_api = (0, 2)

CONFIG_FOLDER = os.path.join(os.path.expanduser('~'), '.cozyfuse')
HDLR = logging.FileHandler(os.path.join(CONFIG_FOLDER, 'cozyfuse.log'))
HDLR.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))

logger = logging.getLogger(__name__)
logger.addHandler(HDLR)
logger.setLevel(logging.INFO)


def get_current_date():
    """
    Get current date : Return current date with format 'Y-m-d T H:M:S'
        Exemple : 2014-05-07T09:17:48
    """
    return datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


def get_date(ctime):
    ctime = ctime[0:24]
    try:
        date = datetime.datetime.strptime(ctime, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        try:
            date = datetime.datetime.strptime(ctime, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            try:
                date = datetime.datetime.strptime(
                    ctime,
                    "%a %b %d %Y %H:%M:%S")
            except ValueError:
                date = datetime.datetime.strptime(
                    ctime,
                    "%a %b %d %H:%M:%S %Y")
    return calendar.timegm(date.utctimetuple())


class CouchStat(fuse.Stat):

    '''
    Default file descriptor.
    '''

    def __init__(self):
        self.st_mode = 0
        self.st_ino = 0
        self.st_dev = 0
        self.st_nlink = 0
        self.st_uid = os.getuid()
        self.st_gid = os.getgid()
        self.st_size = 4096
        self.st_atime = 0
        self.st_mtime = 0
        self.st_ctime = 0
        self.st_blocks = 0


class CouchFSDocument(fuse.Fuse):

    '''
    Fuse implementation behavior: handles synchronisation with device when a
    change occurs or when users want to access to his/her file system.
   '''

    def __init__(self, device_name, mountpoint, uri=None, *args, **kwargs):
        '''
        Configure file system, device and store remote Cozy informations.
        '''
        logger.info('Configuring CouchDB Fuse...')

        # Configure fuse
        fuse.Fuse.__init__(self, *args, **kwargs)
        self.fuse_args.mountpoint = mountpoint
        self.fuse_args.add('allow_other')
        self.currentFile = None
        logger.info('- Fuse configured')

        # Configure device
        self.device = device_name
        (self.db, self.server) = dbutils.get_db_and_server(device_name)
        logger.info('- Database configured')

        # Configure Cozy
        device = dbutils.get_device(device_name)
        self.urlCozy = device['url']
        self.passwordCozy = device['password']
        self.loginCozy = device['login']
        logger.info('- Cozy configured')

        # Configure replication urls.
        (self.db_username, self.db_password) = \
            local_config.get_db_credentials(device_name)
        self.rep_source = 'http://%s:%s@localhost:5984/%s' % (
            self.db_username,
            self.db_password,
            self.device
        )
        self.rep_target = "https://%s:%s@%s/cozy" % (
            self.loginCozy,
            self.passwordCozy,
            self.urlCozy.split('/')[2]
        )
        logger.info(self.rep_target)
        logger.info('- Replication configured')

        # Configure cache and create required folders
        self.writeBuffers = {}
        device_path = os.path.join(CONFIG_FOLDER, device_name)
        self.binary_cache =  binarycache.BinaryCache(
            device_name, device_path, self.rep_source, mountpoint)

        self.file_size_cache = cache.Cache()
        self.attr_cache = cache.Cache(ATTR_VALIDITY_PERIOD)
        self.readdir_file_cache = cache.Cache(ATTR_VALIDITY_PERIOD)
        self.readdir_folder_cache = cache.Cache(ATTR_VALIDITY_PERIOD)
        logger.info('- Cache configured')

    def readdir(self, path, offset):
        """
        Generator: list files for given path and yield each file result when
        it arrives.
        """
        path = _normalize_path(path)
        logger.info('readdir %s' % path)

        # this two folders are conventional in Unix system.
        for directory in '.', '..':
            yield fuse.Direntry(directory)

        res = self.readdir_file_cache.get(path)
        if res is None:
            res = self.db.view('file/byFolder', key=path)
            self.readdir_file_cache.add(path, res)
        for doc in res:
            yield fuse.Direntry(doc.value['name'].encode('utf-8'))

        res = self.readdir_folder_cache.get(path)
        if res is None:
            res = self.db.view('folder/byFolder', key=path)
            self.readdir_folder_cache.add(path, res)
        for doc in res:
            yield fuse.Direntry(doc.value['name'].encode('utf-8'))

    def getattr(self, path):
        """
        Return file descriptor for given_path. Useful for 'ls -la' command
        like.
        """
        logger.info('getattr %s' % path)

        try:
            st = self.attr_cache.get(path)
            if st is None:
                st = CouchStat()

                # Path is root
                if path is "/":
                    st.st_mode = stat.S_IFDIR | 0o775
                    st.st_nlink = 2

                else:
                    # Or path is a folder
                    folder = dbutils.get_folder(self.db, path)

                    if folder is not None:
                        st.st_mode = stat.S_IFDIR | 0o775
                        st.st_nlink = 2
                        if 'lastModification' in folder:
                            st.st_atime = get_date(folder['lastModification'])
                            st.st_ctime = st.st_atime
                            st.st_mtime = st.st_atime

                    else:
                        # Or path is a file
                        file_doc = dbutils.get_file(self.db, path)

                        if file_doc is not None:
                            st.st_mode = stat.S_IFREG | 0o664
                            st.st_nlink = 1
                            st.st_size = file_doc.get('size', 4096)
                            if 'lastModification' in file_doc:
                                st.st_atime = \
                                    get_date(file_doc['lastModification'])
                                st.st_ctime = st.st_atime
                                st.st_mtime = st.st_atime

                        else:
                            logger.info('File does not exist: %s' % path)
                self.attr_cache.add(path, st)
            return st

        except Exception as e:
            logger.exception(e)
            return -errno.ENOENT

    def open(self, path, flags):
        """
        Open file
            path {string}: file path
            flags {string}: opening mode
        """
        logger.info('open %s' % path)
        path = _normalize_path(path)
        try:
            res = self.db.view('file/byFullPath', key=path)
            if len(res) > 0:
                #logger.info('%s found' % path)
                return 0
            else:
                logger.error('File not found %s' % path)
                return -errno.ENOENT

        except Exception as e:
            logger.exception(e)
            return -errno.ENOENT

    def read(self, path, size, offset):
        """
        Return content of file located at given path.
        Extract it from remote Cozy and save it in a cache folder.
            path {string}: file path
            size {integer}: size of file part to read
            offset {integer}=: beginning of file part to read
        """
        try:
            logger.info('read %s' % path)
            path = _normalize_path(path)

            if not self.binary_cache.is_cached(path):
                self.binary_cache.add(path)

            with self.binary_cache.get(path) as binary_attachment:

                content_length = self.file_size_cache.get(path)
                if content_length is None:
                    fileno = binary_attachment.fileno()
                    content_length = os.fstat(fileno).st_size
                    self.file_size_cache.add(path, content_length)

                if offset < content_length:
                    if offset + size > content_length:
                        size = content_length - offset
                    binary_attachment.seek(offset)
                    buf = binary_attachment.read(size)
                else:
                    buf = ''

            return buf

        except Exception as e:
            logger.exception(e)
            return -errno.ENOENT

    def write(self, path, buf, offset):
        """
        Write data in file located at given path.
            path {string}: file path
            buf {buffer}: data to write
        """
        logger.info('write %s' % path)
        return errno.EACCES
        #path = _normalize_path(path)
        #logger.info('write %s' % path)
        #if path not in self.writeBuffers:
            #self.writeBuffers[path] = ''
        #self.writeBuffers[path] = self.writeBuffers[path] + buf
        #return len(buf)

    def release(self, path, fuse_file_info):
        """
        Save file to device and launch replication to remote Cozy.
            path {string}: file path
            fuse_file_info {struct}: information about open file

            Release is called when there are no more references
            to an open file: all file descriptors are closed and
            all memory mappings are unmapped.
        """
        logger.info('release %s' % path)

        return 0
        try:
            path = _normalize_path(path)
            file_doc = dbutils.get_file(self.db, path)
            binary_id = file_doc["binary"]["file"]["id"]

            if path in self.writeBuffers:
                data = self.writeBuffers[path]
                self.db.put_attachment(self.db[binary_id],
                                       data,
                                       filename="file")
                file_doc['size'] = len(data)
                file_doc['lastModification'] = get_current_date()
                self.writeBuffers.pop(path, None)

                binary = self.db[binary_id]
                file_doc['binary']['file']['rev'] = binary['_rev']
                self.db.save(file_doc)

            logger.info("release is done")
            return 0

        except Exception as e:
            logger.exception(e)
            return -errno.ENOENT

    def mknod(self, path, mode, dev):
        """
        Create special/ordinary file. Since it's a new file, the file and
        and the binary metadata are created in the device. Then file is saved
        as an attachment to the device.
            path {string}: file path
            mode {string}: file permissions
            dev: if the file type is S_IFCHR or S_IFBLK, dev specifies the
                 major and minor numbers of the newly created device special
                 file
        """
        logger.info('mknod %s' % path)
        return errno.EACCES
        #try:
            #path = _normalize_path(path)
            #logger.info('mknod %s' % path)
            #(file_path, name) = _path_split(path)

            #file_path = _normalize_path(file_path)
            #new_binary = {"docType": "Binary"}
            #binary_id = self.db.create(new_binary)
            #self.db.put_attachment(self.db[binary_id], '', filename="file")
            #(mime_type, encoding) = mimetypes.guess_type(path)

            #rev = self.db[binary_id]["_rev"]
            #now = get_current_date()
            #newFile = {
                #"name": name,
                #"path": _normalize_path(file_path),
                #"binary": {
                    #"file": {
                        #"id": binary_id,
                        #"rev": rev
                    #}
                #},
                #"docType": "File",
                #"mime": mime_type,
                #'creationDate': now,
                #'lastModification': now,
            #}
            #self.db.create(newFile)
            #logger.info("file created")
            #self._update_parent_folder(newFile['path'])
            #logger.info('mknod is done for %s' % path)
            #return 0
        #except Exception as e:
            #logger.exception(e)
            #return -errno.ENOENT

    def unlink(self, path):
        """
        Remove file from device.
        """

        logger.info('unlink %s' % path)
        return errno.EACCES
        #try:
            #path = _normalize_path(path)
            #logger.info('unlink %s' % path)
            #parts = path.rsplit(u'/', 1)
            #if len(parts) == 1:
                #dirname, filename = u'', parts[0]
            #else:
                #dirname, filename = parts

            #file_doc = dbutils.get_file(self.db, path)
            #if file_doc is not None:
                #binary_id = file_doc["binary"]["file"]["id"]
                #logger.info(self.db[file_doc["_id"]])

                #try:
                    #self.db.delete(self.db[binary_id])
                #except ResourceNotFound:
                    #pass
                #self.db.delete(self.db[file_doc["_id"]])
                #logger.info('file %s removed' % path)
                #self._update_parent_folder(file_doc['path'])
                #return 0
            #else:
                #logger.warn('Cannot delete file, no entry found')
                #return -errno.ENOENT

        #except Exception as e:
            #logger.exception(e)
            #return -errno.ENOENT

    def truncate(self, path, size):
        """ TODO: look if something should be done there.
        Change size of a file.
        """
        return 0

    def utime(self, path, times):
        """ TODO: look if something should be done there.
        Change the access and/or modification times of a file
        """
        return 0

    def mkdir(self, path, mode):
        """
        Create folder in the device.
            path {string}: diretory path
            mode {string}: directory permissions
        """
        logger.info('mkdir %s' % path)
        return errno.EACCES
        #try:
            #(folder_path, name) = _path_split(path)
            #folder_path = _normalize_path(folder_path)
            #path = _normalize_path(path)
            #now = get_current_date()

            #logger.info('create new dir %s' % path)
            #folder = dbutils.get_folder(self.db, path)
            #if folder is not None:
                #logger.info('folder already exists %s' % path)
                #return -errno.EEXIST
            #else:
                #self.db.create({
                    #"name": name,
                    #"path": folder_path,
                    #"docType": "Folder",
                    #'creationDate': now,
                    #'lastModification': now,
                #})

                #self._update_parent_folder(folder_path)
                #return 0

        #except Exception as e:
            #logger.exception(e)
            #return -errno.EEXIST

    def rmdir(self, path):
        """
        Delete folder from device.
            path {string}: diretory path
        """
        logger.info('rmdir %s' % path)
        return errno.EACCES
        #try:
            #path = _normalize_path(path)
            #folder = dbutils.get_folder(self.db, path)
            #self.db.delete(self.db[folder['_id']])
            #return 0

        #except Exception as e:
            #logger.exception(e)
            #return -errno.ENOENT

    def rename(self, pathfrom, pathto, root=True):
        """
        Rename file and subfiles (if it's a folder) in device.
        """
        logger.info('rename %s' % pathfrom)
        return errno.EACCES
        #logger.info("path rename %s -> %s: " % (pathfrom, pathto))
        #pathfrom = _normalize_path(pathfrom)
        #pathto = _normalize_path(pathto)

        #for doc in self.db.view("file/byFullPath", key=pathfrom):
            #doc = doc.value
            #(file_path, name) = _path_split(pathto)
            #doc.update({
                #"name": name,
                #"path": file_path,
                #"lastModification": get_current_date(
                #)})
            #self.db.save(doc)
            #if root:
                #self._update_parent_folder(file_path)
                ## Change lastModification for file_path_from in case of file
                ## was moved
                #(file_path_from, name) = _path_split(pathfrom)
                #self._update_parent_folder(file_path_from)
            #return 0

        #for doc in self.db.view("folder/byFullPath", key=pathfrom):
            #doc = doc.value
            #(file_path, name) = _path_split(pathto)
            #doc.update({
                #"name": name,
                #"path": file_path,
                #"lastModification": get_current_date()
            #})

            ## Rename all subfiles
            #for res in self.db.view("file/byFolder", key=pathfrom):
                #child_pathfrom = os.path.join(
                    #res.value['path'],
                    #res.value['name'])
                #child_pathto = os.path.join(file_path, name, res.value['name'])
                #self.rename(child_pathfrom, child_pathto, False)

            #for res in self.db.view("folder/byFolder", key=pathfrom):
                #child_pathfrom = os.path.join(
                    #res.value['path'],
                    #res.value['name'])
                #child_pathto = os.path.join(file_path, name, res.value['name'])
                #self.rename(child_pathfrom, child_pathto, False)

            #if root:
                #self._update_parent_folder(file_path)
                ## Change lastModification for file_path_from in case of file
                ## was moved
                #(file_path_from, name) = _path_split(pathfrom)
                #self._update_parent_folder(file_path_from)

            #self.db.save(doc)
            #return 0

    def fsync(self, path, isfsyncfile):
        """ TODO: look if something should be done there. """
        return 0

    def chmod(self, path, mode):
        """ TODO: look if something should be done there. """
        return 0

    def chown(self, path, uid, gid):
        """ TODO: look if something should be done there. """
        return 0

    def statfs(self):
        """
        Should return a tuple with the following 6 elements:
            - blocksize - size of file blocks, in bytes
            - totalblocks - total number of blocks in the filesystem
            - freeblocks - number of free blocks
            - availblocks - number of blocks available to non-superuser
            - totalfiles - total number of file inodes
            - freefiles - nunber of free file inodes

        Feel free to set any of the above values to 0, which tells
        the kernel that the info is not available.
        """
        disk_space = dbutils.get_disk_space(
            self.device,
            self.urlCozy,
            self.loginCozy,
            self.passwordCozy)
        st = fuse.StatVfs()

        blocks = float(disk_space['totalDiskSpace']) * 1000 * 1000
        block_size = 1000
        blocks_free = float(disk_space['freeDiskSpace']) * 1000 * 1000
        blocks_avail = blocks_free

        files = 0
        files_free = 0

        st.f_bsize = block_size
        st.f_frsize = block_size
        st.f_blocks = blocks
        st.f_bfree = blocks_free
        st.f_bavail = blocks_avail
        st.f_files = files
        st.f_ffree = files_free

        return st

    def _replicate_from_local(self, ids):
        '''
        Replicate file modifications to remote Cozy.
        '''
        self.rep = self.server.replicate(
            self.rep_source,
            self.rep_target,
            doc_ids=ids
        )

    def _update_parent_folder(self, parent_folder):
        """
        Update parent folder
            parent_folder {string}: parent folder path

        When a file or a folder is renamed/created/removed, last modification
        date of parent folder should be updated

        """
        folder = dbutils.get_folder(self.db, parent_folder)
        if folder is not None:
            folder['lastModification'] = get_current_date()
            self.db.save(folder)


def _normalize_path(path):
    '''
    Remove trailing slash and/or empty path part.
    ex: /home//user/ becomes /home/user
    '''
    parts = path.split('/')
    parts = [part for part in parts if part != '']
    path = '/'.join(parts)
    if len(path) == 0:
        return ''
    else:
        return '/' + path


def _path_split(path):
    '''
    Split folder path and file name.
    '''
    _normalize_path(path)
    (folder_path, name) = os.path.split(path)
    if folder_path[-1:] == '/':
        folder_path = folder_path[:-(len(name) + 1)]
    return (folder_path, name)


def unmount(path):
    '''
    Unmount folder given Fuse folder.
    '''
    if platform.system() == "Darwin" or platform.system() == "FreeBSD":
        command = ["umount", path]
    else:
        command = ["fusermount", "-u", path]

    # Do not display fail messages at unmounting
    subprocess.call(command, stdout=DEVNULL, stderr=subprocess.STDOUT)
    logger.info('Folder %s unmounted' % path)


def mount(name, path):
    '''
    Mount given folder corresponding to given device.
    '''
    logger.info('Attempt to mount %s' % path)
    fs = CouchFSDocument(name, path, 'http://localhost:5984/%s' % name)
    fs.multithreaded = 0
    logger.info('CouchDB Fuse configured for %s' % path)
    fs.main()
