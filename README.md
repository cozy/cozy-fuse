CouchDB-FUSE
============

A FUSE interface to CouchDB. 


Requirements
------------

 * [Python FUSE bindings](http://fuse.sourceforge.net/)
 * [CouchDB-Python](http://code.google.com/p/couchdb-python/) 0.5 or greater

Configuration
-----

Configure your replication:

    $ cd src
    $ python install.py

On ubuntu you have to add read rights on `/etc/fuse.conf`

    chmod a+r /etc/fuse.conf

Usage
-----

    $ mkdir mnt
    $ python couchmount.py http://localhost:5984/database_name mnt/
    $ ls mnt/
    $ touch mnt/foo
    $ ls mnt/
    foo
    $ 

Happy Couching!
