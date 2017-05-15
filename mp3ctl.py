#!/usr/bin/env python
#
# Manage a media device (MP3 player).
#

import sys
import os
import argparse
import subprocess
import shutil
import re
import fileinput
import tempfile
import logging

class MP3Ctl:
    PL_REFMT_EXT = "m3u8" # fixes Unicode playlists in Rockbox
    PL_REFMT_PREFIX = "/<microSD1>/music" # easy method for making MPD playlists
                                          # work with Rockbox
    ERR_DEVICE = 3
    ERR_INTERNAL = 10

    def __init__(self):
        self.device_dir = {
            "media": os.path.join("/mnt-set", "mp3-sd"),
            "sys":   os.path.join("/mnt-set", "mp3-sys"),
        }
        self.media_loc = {
            "music":     os.path.join(os.environ["HOME"], "media", "music"),
            "playlists": os.path.join(os.environ["HOME"], "media", "music-etc", "playlists"),
            "lyrics":    os.path.join(os.environ["HOME"], "media", "music-etc", "lyrics"),
        }

        self.root_tmpdir = tempfile.mkdtemp(prefix="tmp-{}-".format(os.path.basename(__file__)))

    ## CLI-related {{{
    def __init_logging(self):
        self.logger = logging.getLogger(os.path.basename(sys.argv[0]))
        lh = logging.StreamHandler()
        lh.setFormatter(logging.Formatter("%(name)s: %(levelname)s: %(message)s"))
        self.logger.addHandler(lh)

    def __parse_args(self):
        self.parser = argparse.ArgumentParser(description="Manage and maintain a music library.")
        self.parser.add_argument("-v", "--verbose", help="be verbose", action="count", default=0)
        self.parser.add_argument("-q", "--quiet", help="be quiet (overrides -v)", action="count", default=0)
        self.parser.add_argument("command", help="command to run")
        self.parser.add_argument("arguments", nargs="*", help="arguments for command")

        self.args = self.parser.parse_args()
        if self.args.verbose == 0:
            self.logger.setLevel(logging.INFO)
        elif self.args.verbose >= 1:
            self.logger.setLevel(logging.DEBUG)
        if self.args.quiet >= 1:
            self.logger.setLevel(logging.NOTSET)

        # dictionary of command -> function
        # command aliases are easily specified by adding to the key tuple
        self.cmds = {
            ("process-scrobbles", "scrobbles"):
                lambda: self.__show_cmd_help(self.args.arguments),
            ("cp-music", "music"):
                lambda: self.__show_cmd_help(self.args.arguments),
            ("cp-playlists", "playlists"):
                self.cp_playlists,
            ("cp-lyrics", "lyrics"):
                self.cp_lyrics,
            ("help", "h"):
                lambda: self.__show_cmd_help(self.args.arguments),
        }

    def __show_cmd_help(self, args):
        """Show specific command help, or list available commands."""
        if not args:
            print("Available commands: {}".format(", ".join([c[0] for c in self.cmds])))
        else:
            aliases = [c for c in self.cmds.keys() if args[0] in c]
            if not aliases:
                self.exit("unknown command '{}'".format(args[0]), 5)
            aliases = aliases[0]
            print("Command: {}".format(aliases[0]))
            print("Aliases: {}".format(", ".join(aliases[1:])))

    def __parse_cmd(self):
        """Parse commandline command and run a command if found."""
        for cmd_options, cmd_exec in self.cmds.items():
            if self.args.command in cmd_options:
                cmd_exec()
                break
        else:
            self.exit("unknown command '{}'".format(self.args.command), 3)

    def run(self):
        """Run from CLI: parse arguments, execute command."""
        self.__init_logging()
        self.__parse_args()
        self.__parse_cmd()
    ## }}}

    ## Device mount & unmount {{{
    def __ensure_is_device(self, dev):
        if dev not in self.device_dir.keys():
            self.exit("no such configured media device '{}'".format(dev), MP3Ctl.ERR_INTERNAL)

    def mount_dev(self, dev):
        """Try to mount a media device."""
        self.__ensure_is_device(dev)
        mnt_dir = self.device_dir[dev]
        self.logger.info("trying to mount {}...".format(mnt_dir))
        if self.get_shell(["mount", mnt_dir]) == 0:
            self.logger.info("mounted succesfully")
        else:
            self.exit("could not mount directory {}: is the device plugged in?".format(mnt_dir), MP3Ctl.ERR_DEVICE)

    def unmount_dev(self, dev):
        """Try to unmount a media device."""
        self.__ensure_is_device(dev)
        mnt_dir = self.device_dir[dev]
        if self.get_shell(["umount", mnt_dir]) == 0:
            self.logger.info("unmounted successfully")
        else:
            self.exit("could not unmount directory {}".format(mnt_dir), MP3Ctl.ERR_DEVICE)
    ## }}}

    def exit(self, msg, ret):
        """Exit with explanation."""
        self.logger.error(msg)
        sys.exit(ret)

    def get_shell(self, args):
        """Run a shell command and return the exit code."""
        return subprocess.run(args).returncode

    def __cp_contents(self, src, dst):
        # note the trailing forward slash: rsync will copy directory contents
        self.get_shell(["rsync", "-av", "--modify-window=10", "{}/".format(src), dst])

    def cp_playlists(self):
        self.mount_dev("media")
        tmpdir = os.path.join(self.root_tmpdir, "playlists")
        os.mkdir(tmpdir)

        # cp to tmpdir, change extension
        for f in os.listdir(self.media_loc["playlists"]):
            shutil.copy(os.path.join(self.media_loc["playlists"], f),
                        os.path.join(tmpdir, os.path.splitext(f)[0] + ".{}".format(MP3Ctl.PL_REFMT_EXT)))

        # apply line prefix
        for f in os.listdir(tmpdir):
            for line in fileinput.input([os.path.join(tmpdir, f)], inplace=True):
                sys.stdout.write("{}/{}".format(MP3Ctl.PL_REFMT_PREFIX, line))

        self.__cp_contents(tmpdir, os.path.join(self.device_dir["media"], "playlists"))
        self.unmount_dev("media")

    def cp_lyrics(self):
        self.mount_dev("media")
        tmpdir = os.path.join(self.root_tmpdir, "lyrics")
        os.mkdir(tmpdir)

        # cp to tmpdir
        for f in os.listdir(self.media_loc["lyrics"]):
            shutil.copy(os.path.join(self.media_loc["lyrics"], f),
                        os.path.join(tmpdir, f))

        # change naming scheme: "artist - title.txt" -> "title.txt"
        track_split = re.compile(r"(.*) - (.*).txt")
        for f in os.listdir(tmpdir):
            match = track_split.match(f)
            track_artist = match[1]
            track_title = match[2]
            shutil.move(os.path.join(tmpdir, f), os.path.join(tmpdir, track_title) + ".txt")

        self.__cp_contents(tmpdir, os.path.join(self.device_dir["media"], "lyrics"))
        self.unmount_dev("media")


if __name__ == "__main__":
    mp3ctl = MP3Ctl()
    mp3ctl.run()
