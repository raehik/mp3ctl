mp3ctl
======

Control program/manager for my MP3 player, replacing a bunch of separate Bash
scripts.


Dependencies
------------

  * `pylast`


Usage
-----

For scrobbling, make sure you have the config file `mp3ctl.ini` at
`$XDG_CONFIG_HOME` containing the required settings (see the
`MP3Ctl.__init_scrobbler()` function).
