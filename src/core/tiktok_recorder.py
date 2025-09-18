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

def record_with_ffmpeg(live_url, output, duration=None):
    try:
        cmd = [
            "ffmpeg",
            "-y",               # overwrite output if exists
            "-i", live_url,     # live stream url
            "-c", "copy",       # copy without re-encoding
            "-f", "mp4",        # force mp4 container
            output
        ]
        if duration:
            cmd.insert(1, "-t")
            cmd.insert(2, str(duration))  # duration in seconds

        subprocess.run(cmd, check=True)
    except Exception as e:
        print(f"FFmpeg error: {e}")
        
def notify(title, content, ongoing=False):
    """إرسال إشعار في Termux"""
    try:
        cmd = ["termux-notification", "--title", title, "--content", content]
        if ongoing:
            cmd.append("--ongoing")
        subprocess.run(cmd, check=False)
    except Exception:
        pass


class TikTokRecorder:
    def __init__(
        self,
        url,
        user,
        room_id,
        mode,
        automatic_interval,
        cookies,
        proxy,
        output,
        duration,
        use_telegram,
    ):
        self.tiktok = TikTokAPI(proxy=proxy, cookies=cookies)
        self.url = url
        self.user = user
        self.room_id = room_id
        self.mode = mode
        self.automatic_interval = automatic_interval
        self.duration = duration
        self.output = output
        self.use_telegram = use_telegram

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
        active_recordings = {}
        while True:
            try:
                followers = self.tiktok.get_followers_list(self.sec_uid)
                for follower in followers:
                    if follower in active_recordings and active_recordings[follower].is_alive():
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
            except Exception as ex:
                logger.error(f"Unexpected error: {ex}\n")

def start_recording(self, user, room_id):
    live_url = self.tiktok.get_live_url(room_id)
    if not live_url:
        raise LiveNotFound("Could not retrieve live URL")

    current_date = time.strftime("%Y.%m.%d_%H-%M-%S", time.localtime())
    output = f"{self.output if self.output else ''}TK_{user}_{current_date}.mp4"

    logger.info(f"Recording @{user} with FFmpeg...")

    if self.duration:
        notify("TikTok Recorder", f"Recording {self.user} for {self.duration}s", ongoing=True)
        record_with_ffmpeg(live_url, output, self.duration)
    else:
        notify("TikTok Recorder", f"Recording {self.user}", ongoing=True)
        record_with_ffmpeg(live_url, output)

    logger.info(f"Recording finished: {output}")
    notify("TikTok Recorder", f"Finished recording {self.user}", ongoing=False)

    if self.use_telegram:
        Telegram().upload(output)


    def check_country_blacklisted(self):
        is_blacklisted = self.tiktok.is_country_blacklisted()
        if not is_blacklisted:
            return False
        if self.room_id is None:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED)
        if self.mode == Mode.AUTOMATIC:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED_AUTO_MODE)
        elif self.mode == Mode.FOLLOWERS:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED_FOLLOWERS_MODE)
        return is_blacklisted
