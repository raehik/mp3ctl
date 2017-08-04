#!/usr/bin/env python
#
# Manage a media device (MP3 player).
#

import sys, os, argparse, subprocess, logging
import shutil
import re
import fileinput
import tempfile
import time, datetime
import glob

class MP3Ctl:
    MUSCTL = "musctl.py"
    PL_REFMT_EXT = "m3u8" # fixes Unicode playlists in Rockbox
    PL_REFMT_PREFIX = "/<microSD1>/music" # easy method for making MPD playlists
                                          # work with Rockbox
    SCROB_LOG = ".scrobbler.log"
    SCROB_LOG_ARCHIVE_FILE = "{}-scrobbler-log.txt".format(time.strftime("%F-%T"))

    ERR_DEVICE = 3
    ERR_ARGS = 4
    ERR_SCROBBLER = 5
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
            "scrobbles": os.path.join(os.environ["HOME"], "media", "music-etc", "mp3-scrobbles"),
            "podcasts":  os.path.join(os.environ["HOME"], "media", "podcasts", "archive"),
        }

        self.root_tmpdir = tempfile.mkdtemp(prefix="tmp-{}-".format(os.path.basename(__file__)))

    def __deinit(self):
        shutil.rmtree(self.root_tmpdir)

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
        subparsers = self.parser.add_subparsers(title="commands", dest="command", metavar="[command]")
        subparsers.required = True

        subp_scrob = subparsers.add_parser("process-scrobbles",
                aliases=["scrobble"],
                help="process one or more scrobble logs",
                description="By default, archive and scrobble the MP3 player's scrobble log. If arguments are given, leave the MP3 player and scrobble the given logs in place.")
        subp_scrob.add_argument("file", nargs="*", help="logs to scrobble instead of the MP3 player log")
        subp_scrob.add_argument("-e", "--edit", help="edit logs before scrobbling", action="store_true")
        subp_scrob.set_defaults(func=self.process_scrobbles)

        subp_music = subparsers.add_parser("cp-music",
                aliases=["music"],
                help="music -> MP3 player",
                description="Copy full music library to MP3 player.")
        subp_music.set_defaults(func=self.cp_music)

        subp_pl = subparsers.add_parser("cp-playlists",
                aliases=["playlists"],
                help="playlists -> MP3 player",
                description="Copy all playlists to MP3 player.")
        subp_pl.set_defaults(func=self.cp_playlists)

        subp_lyrics = subparsers.add_parser("cp-lyrics",
                aliases=["lyrics"],
                help="lyrics -> MP3 player",
                description="Copy all lyric files to MP3 player.")
        subp_lyrics.set_defaults(func=self.cp_lyrics)

        subp_podcasts = subparsers.add_parser("cp-podcasts",
                help="podcasts -> MP3 player",
                aliases=["podcasts"],
                description="Copy select podcasts (defined in MP3Ctl) to MP3 player.")
        subp_podcasts.set_defaults(func=self.cp_podcasts)

        subp_all = subparsers.add_parser("all",
                help="run all maintenance commands",
                description="Run all maintenance commands.")
        subp_all.set_defaults(func=self.cmd_all)

        self.args = self.parser.parse_args()
        if self.args.verbose == 0:
            self.logger.setLevel(logging.INFO)
        elif self.args.verbose >= 1:
            self.logger.setLevel(logging.DEBUG)
        if self.args.quiet >= 1:
            self.logger.setLevel(logging.NOTSET)

        self.args.func()

    def run(self):
        """Run from CLI: parse arguments, execute command, deinitialise."""
        self.__init_logging()
        self.__parse_args()
        self.__deinit()
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
        self.logger.info("trying to unmount {}...".format(mnt_dir))
        if self.get_shell(["umount", mnt_dir]) == 0:
            self.logger.info("unmounted successfully")
        else:
            self.exit("could not unmount directory {}".format(mnt_dir), MP3Ctl.ERR_DEVICE)
    ## }}}

    def exit(self, msg, ret):
        """Exit with explanation."""
        self.logger.error(msg)
        self.logger.info("deinitialising...")
        self.__deinit()
        sys.exit(ret)

    def get_shell(self, args):
        """Run a shell command and return the exit code."""
        return subprocess.run(args).returncode

    def __cp_contents(self, src, dst):
        # note the trailing forward slash: rsync will copy directory contents
        self.get_shell(["rsync", "-av", "--modify-window=10", "{}/".format(src), dst])

    def cp_playlists(self):
        self.get_shell([MP3Ctl.MUSCTL, "deduplicate-playlists"])
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

        self.mount_dev("media")
        self.__cp_contents(tmpdir, os.path.join(self.device_dir["media"], "playlists"))
        self.unmount_dev("media")

    def cp_lyrics(self):
        tmpdir = os.path.join(self.root_tmpdir, "lyrics")
        os.mkdir(tmpdir)

        self.logger.info("copying lyrics to a temporary dir...")
        for f in os.listdir(self.media_loc["lyrics"]):
            with open(os.path.join(self.media_loc["lyrics"], f)) as f_handle:
                # don't copy lyrics for instrumental songs with no notes
                if f_handle.read().strip() == "[instrumental]":
                    continue
            shutil.copy(os.path.join(self.media_loc["lyrics"], f),
                        os.path.join(tmpdir, f))

        # change naming scheme: "artist - title.txt" -> "title.txt"
        self.logger.info("renaming lyric files...")
        track_split = re.compile(r"(.*) - (.*).txt")
        for f in os.listdir(tmpdir):
            match = track_split.match(f)
            if match == None:
                # not the naming scheme we expected: leave as-is
                continue
            track_artist = match[1]
            track_title = match[2]
            shutil.move(os.path.join(tmpdir, f), os.path.join(tmpdir, track_title) + ".txt")

        self.mount_dev("media")
        self.__cp_contents(tmpdir, os.path.join(self.device_dir["media"], "lyrics"))
        self.unmount_dev("media")

    def cp_music(self):
        self.mount_dev("media")
        self.__cp_contents(self.media_loc["music"], os.path.join(self.device_dir["media"], "music"))
        self.unmount_dev("media")

    def __podcasts_mount_sshfs(self):
        remote_host = "raehik.net"
        remote_port = "6176"
        remote_dir = "media/podcasts"

        if os.path.exists(self.media_loc["podcasts"]):
            self.exit("podcast archive directory already exists", MP3Ctl.ERR_DEVICE)
        os.mkdir(self.media_loc["podcasts"])

        self.logger.info("mounting podcast archive...")
        #self.logger.info("mounting {}:{} -p {} read-only...".format(remote_host, remote_dir, remote_port))
        self.get_shell(["sshfs", "-o", "ro", "-p", remote_port,
            "{}:{}".format(remote_host, remote_dir),
            self.media_loc["podcasts"]])

    def __podcasts_unmount_sshfs(self):
        self.logger.info("unmounting podcast archive...")
        self.get_shell(["fusermount", "-u", self.media_loc["podcasts"]])
        os.rmdir(self.media_loc["podcasts"])

    def cp_podcasts(self):
        self.__podcasts_mount_sshfs()

        ## Podcast: NHK Radio News {{{
        p1_src = os.path.join("nhk-radio-news", "episodes")
        p1_dest = "nhk-radio-news"

        p1_src_abs = os.path.join(self.media_loc["podcasts"], p1_src)
        p1_dest_abs = os.path.join(self.device_dir["media"], "podcasts", p1_dest)
        d_today = datetime.datetime.now().strftime("%Y%m%d")
        d_yest = (datetime.datetime.now() - datetime.timedelta(1)).strftime("%Y%m%d")
        d_two_days = (datetime.datetime.now() - datetime.timedelta(2)).strftime("%Y%m%d")
        d_tomorrow = (datetime.datetime.now() - datetime.timedelta(-2)).strftime("%Y%m%d")

        # select files to copy via date globs
        p1_selected = []
        for glob_path in [os.path.join(p1_src_abs, p) for p in ("{}*".format(d_today), "{}*".format(d_yest), "{}*".format(d_two_days))]:
            p1_selected.extend(glob.glob(glob_path))
        if len(p1_selected) == 0:
            self.logger.info("no podcasts selected, exiting without mounting")
            self.__podcasts_unmount_sshfs()
            return
        ## }}}

        self.mount_dev("media")

        shutil.rmtree(p1_dest_abs)
        os.mkdir(p1_dest_abs)

        rsync_cmd = ["rsync", "-a", "--progress"]
        rsync_cmd.extend(p1_selected)
        rsync_cmd.append(p1_dest_abs)
        self.get_shell(rsync_cmd)

        self.unmount_dev("media")
        self.__podcasts_unmount_sshfs()

    def process_scrobbles(self):
        log_list = []
        if hasattr(self.args, "file") and len(self.args.file) >= 1:
            for f in self.args.file:
                if not os.path.isfile(f):
                    self.exit("not a file: {}".format(f), MP3Ctl.ERR_ARGS)
                log_list.append(f)
        else:
            log_archive_file = os.path.join(self.media_loc["scrobbles"], MP3Ctl.SCROB_LOG_ARCHIVE_FILE)
            self.mount_dev("sys")
            try:
                # archive log
                shutil.move(os.path.join(self.device_dir["sys"], MP3Ctl.SCROB_LOG),
                            log_archive_file)
            except FileNotFoundError:
                self.logger.info("no scrobbler log present")
                self.unmount_dev("sys")
                return
            self.unmount_dev("sys")

            # remove exec. bit
            self.get_shell(["chmod", "-x", log_archive_file])

            self.logger.info("log moved from device -> {}".format(log_archive_file))
            log_list.append(log_archive_file)

        for log in log_list:
            self.logger.info("scrobbling {}...".format(log))
            if hasattr(self.args, "edit") and self.args.edit:
                self.logger.info("editing {}...".format(log))
                self.get_shell([os.getenv("EDITOR", "vim"), log])
            shutil.copy(log, os.path.join(self.root_tmpdir, MP3Ctl.SCROB_LOG))
            ret = self.get_shell(["qtscrob-cli", "--file", "--location", self.root_tmpdir])
            if ret != 0:
                self.exit("qtscrob-cli failed, exiting", MP3Ctl.ERR_SCROBBLER)

    def cmd_all(self):
        self.process_scrobbles()
        self.cp_playlists()
        self.cp_lyrics()
        self.cp_music()

if __name__ == "__main__":
    mp3ctl = MP3Ctl()
    mp3ctl.run()
