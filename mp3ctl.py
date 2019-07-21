#!/usr/bin/env python
#
# Manage a media device (MP3 player).
#

import raehutils
import sys, os, argparse, logging
import subprocess

import shutil
import re
import fileinput
import tempfile
import time, datetime
import glob
import pylast
import configparser

logger = logging.getLogger(os.path.basename(sys.argv[0]))
#lh = logging.StreamHandler()
#lh.setFormatter(logging.Formatter("%(name)s: %(levelname)s: %(message)s"))
#logger.addHandler(lh)

def get_shell(cmd, cwd=None, shell=False):
    """Run a shell command, blocking execution, detaching stdin, stdout and
    stderr.

    Useful for grabbing shell command outputs, or if you want to run something
    silently and wait for it to finish.

    @param cmd command to run as an array, where each element is an argument
    @param cwd if present, directory to use as CWD
    @return the command's return code, stdout and stderr (respectively, as a
            tuple)
    """
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,
                               cwd=cwd,
                               shell=shell)
    return proc.returncode, \
           proc.stdout.decode("utf-8", "replace").strip(), \
           proc.stderr.decode("utf-8", "replace").strip()

class MountDevice():
    def __init__(self, device):
        self.device = device
        self.__reset_mountpoint()

    def __reset_mountpoint(self):
        self.mountpoint = ""

    def __set_mountpoint(self):
        rc, cmd_out, _ = get_shell("udisksctl info -b \"{}\" | sed -n 's/ *MountPoints: *\\(.*$\\)/\\1/p'".format(self.device), shell=True)
        if rc != 0:
            raise Exception("couldn't find mountpoint for (assumed mounted) device {}".format(self.device))
        self.mountpoint = cmd_out

    def get_mountpoint(self):
        return self.mountpoint

    def get_device_name(self):
        return self.device

    def mount(self):
        """Mount this device and set its mountpoint."""
        logger.debug("mounting device {} ...".format(self.device))
        rc = get_shell(["udisksctl", "mount", "-b", self.device])[0]
        if rc != 0:
            raise Exception("could not mount device {} , is the device plugged in?".format(self.device))
        self.__set_mountpoint()

    def unmount(self):
        """Unmount this device."""
        logger.debug("unmounting device {} ...".format(self.device))
        rc = get_shell(["udisksctl", "unmount", "-b", self.device])[0]
        if rc != 0:
            raise Exception("could not unmount device {}".format(self.device))
        self.__reset_mountpoint()

class MP3Ctl(raehutils.RaehBaseClass):
    DEF_CONFIG_FILE = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expandvars("$HOME/.config"), "mp3ctl", "config.ini")

    DEF_MUSCTL = "musctl.py"
    DEF_CONVERTED_EXTS = ["flac"]

    PL_REFMT_EXT = "m3u8" # fixes Unicode playlists in Rockbox
    PL_REFMT_PREFIX = "/<microSD1>/music" # easy method for making MPD playlists
                                          # work with Rockbox
    SCROB_LOG = ".scrobbler.log"
    SCROB_LOG_ARCHIVE_FILE = "{}-scrobbler-log.txt".format(time.strftime("%F-%T"))

    LYRICS_UNWANTED = [ "[instrumental]", "[not found]" ]

    ERR_DEVICE    = 1
    ERR_ARGS      = 2
    ERR_SCROBBLER = 3
    ERR_INTERNAL  = 4
    ERR_MUSCTL    = 5
    ERR_RSYNC     = 6
    ERR_CONFIG    = 7

    def __init__(self):
        self.root_tmpdir = tempfile.mkdtemp(prefix="tmp-{}-".format(os.path.basename(__file__)))
        self.logger = logger

    def _deinit(self):
        self.logger.debug("deinitialising...")
        # TODO: maybe deinit scrobbler if used?
        shutil.rmtree(self.root_tmpdir)

    ## CLI-related {{{
    def _parse_args(self):
        self.parser = argparse.ArgumentParser(description="Manage and maintain a music library.")
        self.parser.add_argument("-v",  "--verbose",    help="be verbose", action="count", default=0)
        self.parser.add_argument("-q",  "--quiet",      help="be quiet (overrides -v)", action="count", default=0)
        self.parser.add_argument("-c",  "--config",     help="specify configuration file", metavar="FILE", default=MP3Ctl.DEF_CONFIG_FILE)
        self.parser.add_argument("-L",  "--copy-links", help="when copying, transform symlink into referent file/dir", action="store_true")
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
        self._read_config()

    def _read_config(self):
        config_file = self.args.config
        self.config = configparser.ConfigParser()
        self.config.read(config_file)

        try:
            self.musctl_bin = self.config["General"]["musctl"]
        except (AttributeError, KeyError):
            self.musctl_bin = MP3Ctl.DEF_MUSCTL
        try:
            converted_exts_str = self.config["General"]["musctl_converted_exts"]
            self.converted_exts = converted_exts_str.split(",")
        except (AttributeError, KeyError):
            self.converted_exts = MP3Ctl.DEF_CONVERTED_EXTS

        self.media_loc = {
            "music":          self._get_general_opt("media_music"),
            "playlists":      self._get_general_opt("media_playlists"),
            "lyrics":         self._get_general_opt("media_lyrics"),
            "scrobbles":      self._get_general_opt("media_scrobbles"),
            "podcasts":       self._get_general_opt("media_podcasts"),
            "music-portable": self._get_general_opt("media_music_portable"),
            "dev-music":      self._get_general_opt("device_music"),
            "dev-playlists":  self._get_general_opt("device_playlists"),
            "dev-lyrics":     self._get_general_opt("device_lyrics"),
            "dev-podcasts":   self._get_general_opt("device_podcasts")
        }

        self.device = {
            "media":  MountDevice(self._get_general_opt("device_media")),
            "system": MountDevice(self._get_general_opt("device_system"))
        }

    def _get_general_opt(self, opt_name):
        try:
            return os.path.expanduser(self.config["General"][opt_name])
        except (AttributeError, KeyError):
            return None

    def _require_locs(self, locs):
        self.logger.warning("location requiring disabled in refactor")
        return True
    ## }}}

    def main(self):
        """Main entrypoint after program initialisation."""
        self.args.func()

    def fail_if_error(self, function_ret, msg, ret):
        """Fail if function_ret is non-zero."""
        if function_ret != 0:
            self.fail(msg, ret)

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

    def cmd_cp_playlists(self):
        self._require_locs([
            #self.device_loc["media"],
            self.media_loc["playlists"],
            self.media_loc["dev-playlists"]
        ])

        self.logger.info("copying playlists to device...")
        self.logger.info("checking playlists with musctl...")
        # TODO: maybe split maintenance cmd into maintenance and maintenance-pl
        self.fail_if_error(
                raehutils.drop_to_shell([self.musctl_bin, "maintenance"]),
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
        self.device["media"].mount()
        self.__cp_dir_contents(
                tmpdir,
                os.path.join(self.device["media"].get_mountpoint(), self.media_loc["dev-playlists"]))
        self.device["media"].unmount()

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
        if hasattr(self.args, "copy_links") and self.args.copy_links:
            cmd_rsync.append("--copy-links")

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
        if hasattr(self.parser, "copy_links") and self.parser.copy_links:
            cmd_rsync.append("--copy-links")

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

    def cmd_cp_lyrics(self):
        self._require_locs([
            #self.device_loc["media"],
            self.media_loc["lyrics"],
            self.media_loc["dev-lyrics"]
        ])

        tmpdir = os.path.join(self.root_tmpdir, "lyrics")
        os.mkdir(tmpdir)

        self.logger.info("filtering unwanted lyrics...")
        for f in os.listdir(self.media_loc["lyrics"]):
            with open(os.path.join(self.media_loc["lyrics"], f)) as f_handle:
                # don't copy unwanted lyrics (instrumental/not found with no
                # notes)
                if f_handle.read().strip() in MP3Ctl.LYRICS_UNWANTED:
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
        self.device["media"].mount()
        self.__cp_dir_contents(tmpdir,
                os.path.join(self.device["media"].get_mountpoint(), self.media_loc["dev-lyrics"]))
        self.device["media"].unmount()

    def cmd_cp_music(self):
        self._require_locs([
            #self.device_loc["media"],
            self.media_loc["music-portable"],
            self.media_loc["dev-music"]
        ])

        self.logger.info("copying music over (from portable library)...")
        self.device["media"].mount()
        self.__cp_dir_contents(
                self.media_loc["music-portable"],
                os.path.join(self.device["media"].get_mountpoint(), self.media_loc["dev-music"]))
        self.device["media"].unmount()

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
        self._require_locs([
            #self.device_loc["media"],
            self.media_loc["podcasts"],
            self.media_loc["dev-podcasts"]
        ])

        self.__podcasts_mount_sshfs()

        ## Podcast: NHK Radio News {{{
        p1_src = os.path.join("nhk-radio-news", "episodes")
        p1_dest = "nhk-radio-news"

        p1_src_abs = os.path.join(self.media_loc["podcasts"], p1_src)
        p1_dest_abs = os.path.join(
                self.device["media"].get_mountpoint(),
                self.media_loc["dev-podcasts"],
                p1_dest)
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

        self.device["media"].mount()

        shutil.rmtree(p1_dest_abs)

        self.__cp_files(p1_selected, p1_dest_abs)

        self.device["media"].unmount()
        self.__podcasts_unmount_sshfs()

    def cmd_process_scrobbles(self):
        self._require_locs([
            #self.device_loc["system"],
            self.media_loc["scrobbles"]
        ])

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
            self.device["system"].mount()
            try:
                # archive log
                shutil.move(
                        os.path.join(self.device["system"].get_mountpoint(), MP3Ctl.SCROB_LOG),
                        log_archive_file)
            except FileNotFoundError:
                self.logger.info("no scrobbler log present")
                self.device["system"].unmount()
                return
            self.device["system"].unmount()

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
