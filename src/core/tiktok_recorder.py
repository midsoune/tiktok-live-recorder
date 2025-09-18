import os
import time
import subprocess
from http.client import HTTPException
from multiprocessing import Process
from requests import RequestException

from core.tiktok_api import TikTokAPI
from utils.logger_manager import logger
from utils.video_management import VideoManagement
from upload.telegram import Telegram
from utils.custom_exceptions import LiveNotFound, UserLiveError, TikTokRecorderError
from utils.enums import Mode, Error, TimeOut, TikTokError


def record_with_ffmpeg(live_url: str, output: str, duration: int = None):
    """
    Record a live stream using ffmpeg and save directly to MP4.
    If duration is provided (seconds) ffmpeg will stop after that time.
    """
    try:
        cmd = ["ffmpeg", "-y"]
        if duration:
            cmd += ["-t", str(int(duration))]
        cmd += ["-i", live_url, "-c", "copy", "-f", "mp4", output]
        logger.info(f"Running ffmpeg: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)
    except Exception as e:
        logger.error(f"FFmpeg error: {e}")
        raise


def notify(title: str, content: str, ongoing: bool = False):
    """Send a Termux notification if available (silent fail otherwise)."""
    try:
        cmd = ["termux-notification", "--title", str(title), "--content", str(content)]
        if ongoing:
            cmd.append("--ongoing")
        subprocess.run(cmd, check=False)
    except Exception:
        # Termux not available or failed — ignore silently
        pass


class TikTokRecorder:
    def __init__(
        self,
        url: str = None,
        user: str = None,
        room_id: str = None,
        mode: Mode = Mode.MANUAL,
        automatic_interval: int = 5,
        cookies: dict = None,
        proxy: str = None,
        output: str = "",
        duration: int = None,
        use_telegram: bool = False,
    ):
        self.tiktok = TikTokAPI(proxy=proxy, cookies=cookies)
        self.url = url
        self.user = user
        self.room_id = room_id
        self.mode = mode
        self.automatic_interval = automatic_interval
        self.duration = duration
        self.output = output or ""
        self.use_telegram = use_telegram

        # validate / adjust output dir
        if isinstance(self.output, str) and self.output != "":
            # ensure trailing slash
            if not (self.output.endswith("/") or self.output.endswith("\\")):
                self.output = self.output + ("\\" if os.name == "nt" else "/")

        # Check blacklist (may raise)
        self.check_country_blacklisted()

        if self.mode == Mode.FOLLOWERS:
            self.sec_uid = self.tiktok.get_sec_uid()
            if self.sec_uid is None:
                raise TikTokRecorderError("Failed to retrieve sec_uid.")
            logger.info("Followers mode activated\n")
        else:
            if self.url:
                self.user, self.room_id = self.tiktok.get_room_and_user_from_url(self.url)
            if not self.user:
                self.user = self.tiktok.get_user_from_room_id(self.room_id)
            if not self.room_id:
                self.room_id = self.tiktok.get_room_id_from_user(self.user)

            logger.info(f"USERNAME: {self.user}" + ("\n" if not self.room_id else ""))
            if self.room_id:
                logger.info(
                    f"ROOM_ID: {self.room_id}"
                    + ("\n" if not self.tiktok.is_room_alive(self.room_id) else "")
                )

        # Reinitialize API without proxy for playback if proxy was only for queries
        if proxy:
            self.tiktok = TikTokAPI(proxy=None, cookies=cookies)

    def run(self):
        if self.mode == Mode.MANUAL:
            self.manual_mode()
        elif self.mode == Mode.AUTOMATIC:
            self.automatic_mode()
        elif self.mode == Mode.FOLLOWERS:
            self.followers_mode()

    def manual_mode(self):
        if not self.tiktok.is_room_alive(self.room_id):
            raise UserLiveError(f"@{self.user}: {TikTokError.USER_NOT_CURRENTLY_LIVE}")
        self.start_recording(self.user, self.room_id)

    def automatic_mode(self):
        while True:
            try:
                self.room_id = self.tiktok.get_room_id_from_user(self.user)
                self.manual_mode()
            except UserLiveError as ex:
                logger.info(ex)
                logger.info(f"Waiting {self.automatic_interval} minutes before recheck\n")
                time.sleep(self.automatic_interval * TimeOut.ONE_MINUTE)
            except LiveNotFound as ex:
                logger.error(f"Live not found: {ex}")
                logger.info(f"Waiting {self.automatic_interval} minutes before recheck\n")
                time.sleep(self.automatic_interval * TimeOut.ONE_MINUTE)
            except ConnectionError:
                logger.error(Error.CONNECTION_CLOSED_AUTOMATIC)
                time.sleep(TimeOut.CONNECTION_CLOSED * TimeOut.ONE_MINUTE)
            except Exception as ex:
                logger.error(f"Unexpected error: {ex}\n")

    def followers_mode(self):
        active_recordings = {}  # follower -> Process
        while True:
            try:
                followers = self.tiktok.get_followers_list(self.sec_uid)
                for follower in followers:
                    # cleanup finished processes
                    if follower in active_recordings:
                        proc = active_recordings[follower]
                        if not proc.is_alive():
                            logger.info(f"Recording of @{follower} finished.")
                            del active_recordings[follower]
                        else:
                            continue

                    try:
                        room_id = self.tiktok.get_room_id_from_user(follower)
                        if not room_id or not self.tiktok.is_room_alive(room_id):
                            continue

                        logger.info(f"@{follower} is live. Starting recording...")
                        process = Process(target=self.start_recording, args=(follower, room_id))
                        process.start()
                        active_recordings[follower] = process
                        time.sleep(2.5)
                    except Exception as e:
                        logger.error(f"Error while processing @{follower}: {e}")
                        continue

                delay = self.automatic_interval * TimeOut.ONE_MINUTE
                logger.info(f"Waiting {delay} minutes for the next check...")
                time.sleep(delay)
            except UserLiveError as ex:
                logger.info(ex)
                logger.info(f"Waiting {self.automatic_interval} minutes before recheck\n")
                time.sleep(self.automatic_interval * TimeOut.ONE_MINUTE)
            except ConnectionError:
                logger.error(Error.CONNECTION_CLOSED_AUTOMATIC)
                time.sleep(TimeOut.CONNECTION_CLOSED * TimeOut.ONE_MINUTE)
            except Exception as ex:
                logger.error(f"Unexpected error: {ex}\n")

    def start_recording(self, user: str, room_id: str):
        """
        Start recording a live stream. Uses ffmpeg to capture directly to mp4.
        """
        try:
            live_url = self.tiktok.get_live_url(room_id)
            if not live_url:
                raise LiveNotFound(TikTokError.RETRIEVE_LIVE_URL)

            current_date = time.strftime("%Y.%m.%d_%H-%M-%S", time.localtime())
            output = os.path.join(self.output, f"TK_{user}_{current_date}.mp4") if self.output else f"TK_{user}_{current_date}.mp4"

            logger.info(f"Recording @{user} with FFmpeg to: {output}")

            # notify start
            if self.duration:
                logger.info(f"Started recording for {self.duration} seconds ")
                notify("TikTok Recorder", f"Recording {user} for {self.duration}s", ongoing=True)
                record_with_ffmpeg(live_url, output, self.duration)
            else:
                logger.info("Started recording...")
                notify("TikTok Recorder", f"Recording {user}", ongoing=True)
                record_with_ffmpeg(live_url, output)

            logger.info(f"Recording finished: {output}")
            notify("TikTok Recorder", f"Finished recording {user}", ongoing=False)

            # conversion (if your pipeline requires conversion, keep it; otherwise skip)
            try:
                # if you produced .mp4 already, conversion may not be needed; this preserves original behavior
                VideoManagement.convert_flv_to_mp4(output)  # safe if it handles mp4 or optional
            except Exception as e:
                logger.debug(f"Conversion skipped or failed: {e}")

            if self.use_telegram:
                try:
                    Telegram().upload(output)
                except Exception as e:
                    logger.error(f"Telegram upload failed: {e}")

        except KeyboardInterrupt:
            logger.info("Recording stopped by user (KeyboardInterrupt).")
            notify("TikTok Recorder", f"Recording stopped: {user}", ongoing=False)
        except LiveNotFound as e:
            logger.error(f"Live not found: {e}")
            notify("TikTok Recorder", f"Live not found: {user}", ongoing=False)
        except Exception as e:
            logger.error(f"Unexpected error during recording: {e}")
            notify("TikTok Recorder", f"Error recording {user}", ongoing=False)

    def check_country_blacklisted(self):
        """
        Check via TikTok API if country is blacklisted. Raises TikTokRecorderError on block.
        """
        try:
            is_blacklisted = self.tiktok.is_country_blacklisted()
        except Exception as e:
            # If the API call fails, assume not blacklisted (or re-raise if you prefer)
            logger.debug(f"Country check failed: {e}")
            is_blacklisted = False

        if not is_blacklisted:
            return False

        if self.room_id is None:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED)

        if self.mode == Mode.AUTOMATIC:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED_AUTO_MODE)
        elif self.mode == Mode.FOLLOWERS:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED_FOLLOWERS_MODE)

        return is_blacklisted
