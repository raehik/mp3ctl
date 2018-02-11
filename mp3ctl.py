#!/usr/bin/env python
#
# Manage a media device (MP3 player).
#

import raehutils
import sys, os, argparse, logging

import shutil
import re
import fileinput
import tempfile
import time, datetime
import glob
import pylast
import configparser

class MP3Ctl(raehutils.RaehBaseClass):
    MUSCTL = "musctl.py"
    PL_REFMT_EXT = "m3u8" # fixes Unicode playlists in Rockbox
    PL_REFMT_PREFIX = "/<microSD1>/music" # easy method for making MPD playlists
                                          # work with Rockbox
    SCROB_LOG = ".scrobbler.log"
    SCROB_LOG_ARCHIVE_FILE = "{}-scrobbler-log.txt".format(time.strftime("%F-%T"))
    CONFIG_FILE = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expandvars("$HOME/.config"), "mp3ctl.ini")

    ERR_DEVICE = 3
    ERR_ARGS = 4
    ERR_SCROBBLER = 5
    ERR_INTERNAL = 10
    ERR_MUSCTL = 11
    ERR_RSYNC = 12

    def __init__(self):
        self.device_dir = {
            "media": os.path.join("/mnt-set", "mp3-sd"),
            "sys":   os.path.join("/mnt-set", "mp3-sys"),
        }
        self.media_loc = {
            "music":     os.path.join(os.environ["HOME"], "media", "music"),
            "music-portable": os.path.join(os.environ["HOME"], "media", "music-etc", "music-portable"),
            "playlists": os.path.join(os.environ["HOME"], "media", "music-etc", "playlists"),
            "lyrics":    os.path.join(os.environ["HOME"], "media", "music-etc", "lyrics"),
            "scrobbles": os.path.join(os.environ["HOME"], "media", "music-etc", "mp3-scrobbles"),
            "podcasts":  os.path.join(os.environ["HOME"], "media", "podcasts", "archive"),
        }

        self.root_tmpdir = tempfile.mkdtemp(prefix="tmp-{}-".format(os.path.basename(__file__)))

        self.converted_exts = ["flac"]

        self.config = configparser.ConfigParser()
        self.config.read(MP3Ctl.CONFIG_FILE)

    def _deinit(self):
        self.logger.debug("deinitialising...")
        # TODO: maybe deinit scrobbler if used?
        shutil.rmtree(self.root_tmpdir)

    ## CLI-related {{{
    def _parse_args(self):
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
        subp_scrob.set_defaults(func=self.cmd_process_scrobbles)

        subp_music = subparsers.add_parser("cp-music",
                aliases=["music"],
                help="music -> MP3 player",
                description="Copy full music library to MP3 player.")
        subp_music.set_defaults(func=self.cmd_cp_music)

        subp_pl = subparsers.add_parser("cp-playlists",
                aliases=["playlists"],
                help="playlists -> MP3 player",
                description="Copy all playlists to MP3 player.")
        subp_pl.set_defaults(func=self.cmd_cp_playlists)

        subp_lyrics = subparsers.add_parser("cp-lyrics",
                aliases=["lyrics"],
                help="lyrics -> MP3 player",
                description="Copy all lyric files to MP3 player.")
        subp_lyrics.set_defaults(func=self.cmd_cp_lyrics)

        subp_podcasts = subparsers.add_parser("cp-podcasts",
                help="podcasts -> MP3 player",
                aliases=["podcasts"],
                description="Copy select podcasts (defined in MP3Ctl) to MP3 player.")
        subp_podcasts.set_defaults(func=self.cmd_cp_podcasts)

        subp_maintenance = subparsers.add_parser("maintenance",
                help="run maintenance commands",
                aliases=["maint"],
                description="Run all maintenance commands.")
        subp_maintenance.set_defaults(func=self.cmd_maintenance)

        self.args = self.parser.parse_args()

        self.args.verbose += 1 # force some verbosity
        self._parse_verbosity()
    ## }}}

    ## Device mount & unmount {{{
    def __ensure_is_device(self, dev):
        if dev not in self.device_dir.keys():
            self.fail("no such configured media device '{}'".format(dev), MP3Ctl.ERR_INTERNAL)

    def mount_dev(self, dev):
        """Try to mount a media device."""
        self.__ensure_is_device(dev)
        mnt_dir = self.device_dir[dev]
        self.logger.debug("trying to mount {}...".format(mnt_dir))
        self.fail_if_error(
                raehutils.get_shell(["mount", mnt_dir])[0],
                "could not mount directory {}: is the device plugged in?".format(mnt_dir),
                MP3Ctl.ERR_DEVICE)

    def unmount_dev(self, dev):
        """Try to unmount a media device."""
        self.__ensure_is_device(dev)
        mnt_dir = self.device_dir[dev]
        self.logger.debug("trying to unmount {}...".format(mnt_dir))
        self.fail_if_error(
                raehutils.get_shell(["umount", mnt_dir])[0],
                "could not unmount directory {}".format(mnt_dir),
                MP3Ctl.ERR_DEVICE)
    ## }}}

    def main(self):
        """Main entrypoint after program initialisation."""
        self.args.func()

    def fail_if_error(self, function_ret, msg, ret):
        """Fail if function_ret is non-zero."""
        if function_ret != 0:
            self.fail(msg, ret)

    def cmd_cp_playlists(self):
        self.logger.info("copying playlists to device...")
        self.logger.info("checking playlists with musctl...")
        # TODO: maybe split maintenance cmd into maintenance and maintenance-pl
        self.fail_if_error(
                raehutils.drop_to_shell([MP3Ctl.MUSCTL, "maintenance"]),
                "error checking playlists with musctl",
                MP3Ctl.ERR_MUSCTL)
        tmpdir = os.path.join(self.root_tmpdir, "playlists")
        os.mkdir(tmpdir)

        # cp to tmpdir, change extension
        for pl in os.listdir(self.media_loc["playlists"]):
            shutil.copy(os.path.join(self.media_loc["playlists"], pl),
                        os.path.join(tmpdir, os.path.splitext(pl)[0] + ".{}".format(MP3Ctl.PL_REFMT_EXT)))

        # apply changes to tracks (prefix, extension)
        for pl in os.listdir(tmpdir):
            tracks = []
            with open(os.path.join(tmpdir, pl), "r+") as f:
                tracks = [self.__edit_playlist_line(line.strip()) for line in f]
                f.seek(0)
                for t in tracks:
                    f.write("{}\n".format(t))
                f.truncate()

        self.logger.info("copying playlists over...")
        self.mount_dev("media")
        self.__cp_dir_contents(tmpdir, os.path.join(self.device_dir["media"], "playlists"))
        self.unmount_dev("media")

    def __edit_playlist_line(self, track):
        # prefix
        track = "{}/{}".format(MP3Ctl.PL_REFMT_PREFIX, track)

        # change ext
        track_stem, track_ext = os.path.splitext(track)
        if track_ext[1:] in self.converted_exts:
            track = track_stem+".ogg"

        return track

    def __cp_dir_contents(self, src, dst):
        """Copies the contents of src to dst.

        @param src a valid existing directory
        @param dst a valid filepath (will be created if not present)
        """
        # TODO: should --modify-window=10 be an optional argument instead?
        cmd_rsync = ["rsync", "-a", "--modify-window=10"]

        # show output depending on verbosity
        if self.args.verbose == 2:
            cmd_rsync.append("--info=progress2")
        elif self.args.verbose >= 3:
            cmd_rsync.append("-P")

        # note the trailing forward slash: rsync will copy directory contents
        cmd_rsync += [src+"/", dst]

        self.fail_if_error(
                self.run_shell_cmd(cmd_rsync, min_verb_lvl=2),
                "rsync copy failed", MP3Ctl.ERR_RSYNC)
        self.logger.info("copy finished")

    def __cp_files(self, files, dst):
        """Copies files to dst.

        @param files a list of existing files
        @param dst   a valid filepath (will be created if not present)
        """
        # TODO: code duplication
        # TODO: should --modify-window=10 be an optional argument instead?
        cmd_rsync = ["rsync", "-a", "--modify-window=10"]

        # show output depending on verbosity
        if self.args.verbose == 2:
            cmd_rsync.append("--info=progress2")
        elif self.args.verbose >= 3:
            cmd_rsync.append("-P")

        cmd_rsync += files + [dst]

        self.fail_if_error(
                self.run_shell_cmd(cmd_rsync, min_verb_lvl=2),
                "rsync copy failed", MP3Ctl.ERR_RSYNC)
        self.logger.info("copy finished successfully")

    def run_shell_cmd(self, cmd, cwd=None, min_verb_lvl=3):
        """Run a shell command, only showing output if sufficiently verbose.

        Assumes that command's output is "optional" in the first place, and
        doesn't require any input.

        @param cmd command to run as an array, where each element is an argument
        @param cwd if present, directory to use as CWD
        @param min_verb_lvl verbosity level required to show output
        @return the command's exit code
        """
        rc = 0
        if self.args.quiet == 0 and self.args.verbose >= min_verb_lvl:
            rc = raehutils.drop_to_shell(cmd, cwd=cwd)
        else:
            rc = raehutils.get_shell(cmd, cwd=cwd)[0]
        return rc

    def cmd_cp_lyrics(self):
        tmpdir = os.path.join(self.root_tmpdir, "lyrics")
        os.mkdir(tmpdir)

        self.logger.info("filtering unwanted lyrics...")
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

        self.logger.info("copying lyrics over...")
        self.mount_dev("media")
        self.__cp_dir_contents(tmpdir, os.path.join(self.device_dir["media"], "lyrics"))
        self.unmount_dev("media")

    def cmd_cp_music(self):
        self.logger.info("copying music over (from portable library)...")
        self.mount_dev("media")
        self.__cp_dir_contents(self.media_loc["music-portable"], os.path.join(self.device_dir["media"], "music"))
        self.unmount_dev("media")

    def __podcasts_mount_sshfs(self):
        remote_host = "raehik.net"
        remote_port = "6176"
        remote_dir = "/mnt/media/podcasts"

        if os.path.exists(self.media_loc["podcasts"]):
            self.fail("podcast archive directory already exists", MP3Ctl.ERR_DEVICE)
        os.mkdir(self.media_loc["podcasts"])

        self.logger.info("mounting podcast archive...")
        self.fail_if_error(
                raehutils.drop_to_shell(["sshfs", "-o", "ro", "-p", remote_port,
                    "{}:{}".format(remote_host, remote_dir),
                    self.media_loc["podcasts"]]),
                "couldn't mount podcast archive", MP3Ctl.ERR_DEVICE)

    def __podcasts_unmount_sshfs(self):
        self.logger.info("unmounting podcast archive...")
        self.fail_if_error(
                raehutils.drop_to_shell(["fusermount", "-u", self.media_loc["podcasts"]]),
                "couldn't unmount podcast archive", MP3Ctl.ERR_DEVICE)
        os.rmdir(self.media_loc["podcasts"])

    def cmd_cp_podcasts(self):
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

        self.__cp_files(p1_selected, p1_dest_abs)

        self.unmount_dev("media")
        self.__podcasts_unmount_sshfs()

    def cmd_process_scrobbles(self):
        self.logger.info("processing scrobbles...")
        log_list = []
        if hasattr(self.args, "file") and len(self.args.file) >= 1:
            for f in self.args.file:
                if not os.path.isfile(f):
                    self.fail("not a file: {}".format(f), MP3Ctl.ERR_ARGS)
                log_list.append(f)
        else:
            self.logger.info("grabbing device scrobble log...")
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
            raehutils.get_shell(["chmod", "-x", log_archive_file])

            self.logger.info("log moved from device -> {}".format(log_archive_file))
            log_list.append(log_archive_file)

        self.__init_scrobbler()

        for log in log_list:
            if hasattr(self.args, "edit") and self.args.edit:
                self.logger.info("editing scrobble log {}...".format(log))
                raehutils.drop_to_shell([os.getenv("EDITOR", "vim"), log])
            self.__submit_scrobble_log(log)

    def __init_scrobbler(self):
        self.scrobbler = pylast.LastFMNetwork(
                api_key=self.config["Scrobbling"]["api_key"],
                api_secret=self.config["Scrobbling"]["api_secret"],
                username=self.config["Scrobbling"]["username"],
                password_hash=self.config["Scrobbling"]["password_hash"])

    def __submit_scrobble_log(self, log):
        self.logger.info("scrobbling {}...".format(log))
        tracks = []
        tracks_count = 0
        listened_count = 0
        with open(log, "r") as f:
            for line in f:
                if line.startswith("#"):
                    # ignore comment lines (found at the top of the file)
                    continue
                tracks_count += 1
                parts = re.split(r"\t", line.rstrip("\r\n"))
                parts = [ None if p == "" else p for p in parts ]
                track = {"artist":    parts[0],
                         "album":     parts[1],
                         "title":     parts[2],
                         "track_num": parts[3],
                         "duration":  parts[4],
                         "status":    parts[5],
                         "timestamp": self.__fix_timestamp(int(parts[6])),
                         "mbid":      parts[7]
                }
                self.logger.debug("{} {} {} - {}".format(track["status"], track["timestamp"], track["artist"], track["title"]))
                if track["status"] == "S":
                    # song was considered skipped, ignore
                    continue
                elif track["status"] == "L":
                    # song was considered listened to, scrobble it
                    tracks.append(track)
                    listened_count += 1
                else:
                    self.fail("misformed scrobble log", MP3Ctl.ERR_SCROBBLER)
        self.scrobbler.scrobble_many(tracks)
        self.logger.info("scrobbled tracks: {} listened, {} total".format(listened_count, tracks_count))

    def __fix_timestamp(self, epoch):
        return datetime.datetime.utcfromtimestamp(epoch).strftime("%s")

    def cmd_maintenance(self):
        self.cmd_process_scrobbles()
        self.cmd_cp_playlists()
        self.cmd_cp_lyrics()
        self.cmd_cp_music()

if __name__ == "__main__":
    mp3ctl = MP3Ctl()
    mp3ctl.run()
