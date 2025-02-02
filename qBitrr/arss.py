from __future__ import annotations

import contextlib
import itertools
import logging
import pathlib
import re
import shutil
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Iterable, Iterator, NoReturn

import ffmpeg
import pathos
import peewee
import qbittorrentapi
import qbittorrentapi.exceptions
import requests
from packaging import version as version_parser
from peewee import JOIN, SqliteDatabase, fn
from pyarr import RadarrAPI, SonarrAPI
from qbittorrentapi import TorrentDictionary, TorrentStates
from ujson import JSONDecodeError

from qBitrr.arr_tables import (
    CommandsModel,
    EpisodesModel,
    MoviesMetadataModel,
    MoviesModel,
    MoviesModelv5,
    SeriesModel,
    SeriesModelv4,
)
from qBitrr.config import (
    APPDATA_FOLDER,
    COMPLETED_DOWNLOAD_FOLDER,
    CONFIG,
    ENABLE_LOGS,
    FAILED_CATEGORY,
    LOOP_SLEEP_TIMER,
    NO_INTERNET_SLEEP_TIMER,
    PROCESS_ONLY,
    QBIT_DISABLED,
    RECHECK_CATEGORY,
    SEARCH_ONLY,
)
from qBitrr.errors import (
    DelayLoopException,
    NoConnectionrException,
    RestartLoopException,
    SkipException,
    UnhandledError,
)
from qBitrr.home_path import HOME_PATH
from qBitrr.logger import run_logs
from qBitrr.tables import (
    EpisodeFilesModel,
    EpisodeQueueModel,
    FilesQueued,
    MovieQueueModel,
    MoviesFilesModel,
    SeriesFilesModel,
)
from qBitrr.utils import (
    ExpiringSet,
    absolute_file_paths,
    has_internet,
    validate_and_return_torrent_file,
)

if TYPE_CHECKING:
    from qBitrr.main import qBitManager


class Arr:
    def __init__(
        self,
        name: str,
        manager: ArrManager,
        client_cls: type[Callable | RadarrAPI | SonarrAPI],
    ):
        if name in manager.groups:
            raise OSError("Group '{name}' has already been registered.")
        self._name = name
        self.managed = CONFIG.get(f"{name}.Managed", fallback=False)
        if not self.managed:
            raise SkipException
        self.uri = CONFIG.get_or_raise(f"{name}.URI")
        if self.uri in manager.uris:
            raise OSError(
                f"Group '{self._name}' is trying to manage Arr instance: "
                f"'{self.uri}' which has already been registered."
            )
        self.category = CONFIG.get(f"{name}.Category", fallback=self._name)
        self.manager = manager
        self._LOG_LEVEL = self.manager.qbit_manager.logger.level
        if ENABLE_LOGS:
            LOGS_FOLDER = HOME_PATH.joinpath("logs")
            LOGS_FOLDER.mkdir(parents=True, exist_ok=True)
            LOGS_FOLDER.chmod(mode=0o777)
            logfile = LOGS_FOLDER.joinpath(self._name + ".log")
            if pathlib.Path(logfile).is_file():
                logold = LOGS_FOLDER.joinpath(self._name + ".log.old")
                logfile.rename(logold)
            fh = logging.FileHandler(logfile)
            self.logger = logging.getLogger(f"qBitrr.{self._name}")
            self.logger.addHandler(fh)
        else:
            self.logger = logging.getLogger(f"qBitrr.{self._name}")
        run_logs(self.logger)
        self.completed_folder = pathlib.Path(COMPLETED_DOWNLOAD_FOLDER).joinpath(self.category)
        if not self.completed_folder.exists() and not SEARCH_ONLY:
            try:
                self.completed_folder.mkdir(parents=True, exist_ok=True)
                self.completed_folder.chmod(mode=0o777)
            except BaseException:
                self.logger.warning(
                    "%s completed folder is a soft requirement. The specified folder does not exist %s and cannot be created. This will disable all file monitoring.",
                    self._name,
                    self.completed_folder,
                )
        self.apikey = CONFIG.get_or_raise(f"{name}.APIKey")
        self.re_search = CONFIG.get(f"{name}.ReSearch", fallback=False)
        self.import_mode = CONFIG.get(f"{name}.importMode", fallback="Move")
        self.refresh_downloads_timer = CONFIG.get(f"{name}.RefreshDownloadsTimer", fallback=1)
        self.arr_error_codes_to_blocklist = CONFIG.get(
            f"{name}.ArrErrorCodesToBlocklist", fallback=[]
        )
        self.rss_sync_timer = CONFIG.get(f"{name}.RssSyncTimer", fallback=15)

        self.case_sensitive_matches = CONFIG.get(
            f"{name}.Torrent.CaseSensitiveMatches", fallback=[]
        )
        self.folder_exclusion_regex = CONFIG.get(
            f"{name}.Torrent.FolderExclusionRegex", fallback=[]
        )
        self.file_name_exclusion_regex = CONFIG.get(
            f"{name}.Torrent.FileNameExclusionRegex", fallback=[]
        )
        self.file_extension_allowlist = CONFIG.get(
            f"{name}.Torrent.FileExtensionAllowlist", fallback=[]
        )
        self.file_extension_allowlist = [
            rf"\{ext}" if ext[:1] != "\\" else ext for ext in self.file_extension_allowlist
        ]
        self.auto_delete = CONFIG.get(f"{name}.Torrent.AutoDelete", fallback=False)

        self.remove_dead_trackers = CONFIG.get(
            f"{name}.Torrent.SeedingMode.RemoveDeadTrackers", fallback=False
        )
        self.seeding_mode_global_download_limit = CONFIG.get(
            f"{name}.Torrent.SeedingMode.DownloadRateLimitPerTorrent", fallback=-1
        )
        self.seeding_mode_global_upload_limit = CONFIG.get(
            f"{name}.Torrent.SeedingMode.UploadRateLimitPerTorrent", fallback=-1
        )
        self.seeding_mode_global_max_upload_ratio = CONFIG.get(
            f"{name}.Torrent.SeedingMode.MaxUploadRatio", fallback=-1
        )
        self.seeding_mode_global_max_seeding_time = CONFIG.get(
            f"{name}.Torrent.SeedingMode.MaxSeedingTime", fallback=-1
        )
        self.seeding_mode_global_remove_torrent = CONFIG.get(
            f"{name}.Torrent.SeedingMode.RemoveTorrent", fallback=-1
        )
        self.seeding_mode_global_bad_tracker_msg = CONFIG.get(
            f"{name}.Torrent.SeedingMode.RemoveTrackerWithMessage", fallback=[]
        )

        self.monitored_trackers = CONFIG.get(f"{name}.Torrent.Trackers", fallback=[])
        self._remove_trackers_if_exists: set[str] = {
            i.get("URI") for i in self.monitored_trackers if i.get("RemoveIfExists") is True
        }
        self._monitored_tracker_urls: set[str] = {
            r
            for i in self.monitored_trackers
            if not (r := i.get("URI")) not in self._remove_trackers_if_exists
        }
        self._add_trackers_if_missing: set[str] = {
            i.get("URI") for i in self.monitored_trackers if i.get("AddTrackerIfMissing") is True
        }
        if (
            self.auto_delete is True
            and not self.completed_folder.parent.exists()
            and not SEARCH_ONLY
        ):
            self.auto_delete = False
            self.logger.critical(
                "AutoDelete disabled due to missing folder: '%s'",
                self.completed_folder.parent,
            )

        self.reset_on_completion = CONFIG.get(
            f"{name}.EntrySearch.SearchAgainOnSearchCompletion", fallback=False
        )
        self.do_upgrade_search = CONFIG.get(f"{name}.EntrySearch.DoUpgradeSearch", fallback=False)
        self.quality_unmet_search = CONFIG.get(
            f"{name}.EntrySearch.QualityUnmetSearch", fallback=False
        )

        self.ignore_torrents_younger_than = CONFIG.get(
            f"{name}.Torrent.IgnoreTorrentsYoungerThan", fallback=600
        )
        self.maximum_eta = CONFIG.get(f"{name}.Torrent.MaximumETA", fallback=86400)
        self.maximum_deletable_percentage = CONFIG.get(
            f"{name}.Torrent.MaximumDeletablePercentage", fallback=0.95
        )
        self.search_missing = CONFIG.get(f"{name}.EntrySearch.SearchMissing", fallback=False)
        if PROCESS_ONLY:
            self.search_missing = False
        self.search_specials = CONFIG.get(f"{name}.EntrySearch.AlsoSearchSpecials", fallback=False)
        self.search_by_year = CONFIG.get(f"{name}.EntrySearch.SearchByYear", fallback=True)
        self.search_in_reverse = CONFIG.get(f"{name}.EntrySearch.SearchInReverse", fallback=False)

        self.search_command_limit = CONFIG.get(f"{name}.EntrySearch.SearchLimit", fallback=5)
        self.prioritize_todays_release = CONFIG.get(
            f"{name}.EntrySearch.PrioritizeTodaysReleases", fallback=True
        )

        self.do_not_remove_slow = CONFIG.get(f"{name}.Torrent.DoNotRemoveSlow", fallback=False)
        self.search_current_year = None
        if self.search_in_reverse:
            self._delta = 1
        else:
            self._delta = -1
        arr_db_file = CONFIG.get(f"{name}.EntrySearch.DatabaseFile", fallback=None)
        self.arr_db_file = pathlib.Path("/.Invalid Place Holder")
        if self.search_missing and arr_db_file is None:
            self.logger.critical("Arr DB file not specified setting SearchMissing to False")
            self.search_missing = False
        if arr_db_file is not None:
            self.arr_db_file = pathlib.Path(arr_db_file)
        self._app_data_folder = APPDATA_FOLDER
        self.search_db_file = self._app_data_folder.joinpath(f"{self._name}.db")
        if self.search_missing and not self.arr_db_file.exists():
            self.logger.critical(
                "Arr DB file cannot be located setting SearchMissing to False: %s",
                self.arr_db_file,
            )
            self.search_missing = False

        self.ombi_search_requests = CONFIG.get(
            f"{name}.EntrySearch.Ombi.SearchOmbiRequests", fallback=False
        )
        self.overseerr_requests = CONFIG.get(
            f"{name}.EntrySearch.Overseerr.SearchOverseerrRequests", fallback=False
        )
        self.series_search = CONFIG.get(f"{name}.EntrySearch.SearchBySeries", fallback=False)
        if self.ombi_search_requests:
            self.ombi_uri = CONFIG.get_or_raise(f"{name}.EntrySearch.Ombi.OmbiURI")
            self.ombi_api_key = CONFIG.get_or_raise(f"{name}.EntrySearch.Ombi.OmbiAPIKey")
        else:
            self.ombi_uri = CONFIG.get(f"{name}.EntrySearch.Ombi.OmbiURI", fallback=None)
            self.ombi_api_key = CONFIG.get(f"{name}.EntrySearch.Ombi.OmbiAPIKey", fallback=None)
        if self.overseerr_requests:
            self.overseerr_uri = CONFIG.get_or_raise(f"{name}.EntrySearch.Overseerr.OverseerrURI")
            self.overseerr_api_key = CONFIG.get_or_raise(
                f"{name}.EntrySearch.Overseerr.OverseerrAPIKey"
            )
        else:
            self.overseerr_uri = CONFIG.get(
                f"{name}.EntrySearch.Overseerr.OverseerrURI", fallback=None
            )
            self.overseerr_api_key = CONFIG.get(
                f"{name}.EntrySearch.Overseerr.OverseerrAPIKey", fallback=None
            )
        self.overseerr_is_4k = CONFIG.get(f"{name}.EntrySearch.Overseerr.Is4K", fallback=False)
        self.ombi_approved_only = CONFIG.get(
            f"{name}.EntrySearch.Ombi.ApprovedOnly", fallback=True
        )
        self.overseerr_approved_only = CONFIG.get(
            f"{name}.EntrySearch.Overseerr.ApprovedOnly", fallback=True
        )
        self.search_requests_every_x_seconds = CONFIG.get(
            f"{name}.EntrySearch.SearchRequestsEvery", fallback=1800
        )
        self._temp_overseer_request_cache: dict[str, set[int | str]] = defaultdict(set)
        if self.ombi_search_requests or self.overseerr_requests:
            self.request_search_timer = 0
        else:
            self.request_search_timer = None

        if self.case_sensitive_matches:
            self.folder_exclusion_regex_re = re.compile(
                "|".join(self.folder_exclusion_regex), re.DOTALL
            )
            self.file_name_exclusion_regex_re = re.compile(
                "|".join(self.file_name_exclusion_regex), re.DOTALL
            )
        else:
            self.folder_exclusion_regex_re = re.compile(
                "|".join(self.folder_exclusion_regex), re.IGNORECASE | re.DOTALL
            )
            self.file_name_exclusion_regex_re = re.compile(
                "|".join(self.file_name_exclusion_regex), re.IGNORECASE | re.DOTALL
            )
        self.file_extension_allowlist = re.compile(
            "|".join(self.file_extension_allowlist), re.DOTALL
        )
        self.client = client_cls(host_url=self.uri, api_key=self.apikey)
        if isinstance(self.client, SonarrAPI):
            self.type = "sonarr"
            version_info = self.client.get_update()
            self.version = version_parser.parse(version_info[0].get("version"))
            self.logger.debug("%s version: %s", self._name, self.version.__str__())
        elif isinstance(self.client, RadarrAPI):
            self.type = "radarr"
            version_info = self.client.get_update()
            self.version = version_parser.parse(version_info[0].get("version"))
            self.logger.debug("%s version: %s", self._name, self.version.__str__())

        if self.rss_sync_timer > 0:
            self.rss_sync_timer_last_checked = datetime(1970, 1, 1)
        else:
            self.rss_sync_timer_last_checked = None
        if self.refresh_downloads_timer > 0:
            self.refresh_downloads_timer_last_checked = datetime(1970, 1, 1)
        else:
            self.refresh_downloads_timer_last_checked = None

        self.loop_completed = False
        self.queue = []
        self.cache = {}
        self.requeue_cache = {}
        self.queue_file_ids = set()
        self.sent_to_scan = set()
        self.sent_to_scan_hashes = set()
        self.files_probed = set()
        self.import_torrents = []
        self.change_priority = dict()
        self.recheck = set()
        self.pause = set()
        self.skip_blacklist = set()
        self.delete = set()
        self.resume = set()
        self.remove_from_qbit = set()
        self.overseerr_requests_release_cache = dict()
        self.files_to_explicitly_delete: Iterator = iter([])
        self.files_to_cleanup = set()
        self.missing_files_post_delete = set()
        self.downloads_with_bad_error_message_blocklist = set()
        self.needs_cleanup = False
        self.recently_queue = dict()

        self.timed_ignore_cache = ExpiringSet(max_age_seconds=self.ignore_torrents_younger_than)
        self.timed_skip = ExpiringSet(max_age_seconds=self.ignore_torrents_younger_than)
        self.tracker_delay = ExpiringSet(max_age_seconds=600)
        self.special_casing_file_check = ExpiringSet(max_age_seconds=10)
        self.expiring_bool = ExpiringSet(max_age_seconds=10)
        self.session = requests.Session()
        self.cleaned_torrents = set()
        self.search_api_command = None

        self.manager.completed_folders.add(self.completed_folder)
        self.manager.category_allowlist.add(self.category)

        self.logger.debug(
            "%s Config: "
            "Managed: %s, "
            "Re-search: %s, "
            "ImportMode: %s, "
            "Category: %s, "
            "URI: %s, "
            "API Key: %s, "
            "RefreshDownloadsTimer=%s, "
            "RssSyncTimer=%s",
            self._name,
            self.import_mode,
            self.managed,
            self.re_search,
            self.category,
            self.uri,
            self.apikey,
            self.refresh_downloads_timer,
            self.rss_sync_timer,
        )
        self.logger.debug(
            "Script Config:  CaseSensitiveMatches=%s",
            self.case_sensitive_matches,
        )
        self.logger.debug(
            "Script Config:  FolderExclusionRegex=%s",
            self.folder_exclusion_regex,
        )
        self.logger.debug(
            "Script Config:  FileNameExclusionRegex=%s",
            self.file_name_exclusion_regex,
        )
        self.logger.debug(
            "Script Config:  FileExtensionAllowlist=%s",
            self.file_extension_allowlist,
        )
        self.logger.debug("Script Config:  AutoDelete=%s", self.auto_delete)

        self.logger.debug(
            "Script Config:  IgnoreTorrentsYoungerThan=%s",
            self.ignore_torrents_younger_than,
        )
        self.logger.debug("Script Config:  MaximumETA=%s", self.maximum_eta)

        if self.search_missing:
            self.logger.debug(
                "Script Config:  SearchMissing=%s",
                self.search_missing,
            )
            self.logger.debug(
                "Script Config:  AlsoSearchSpecials=%s",
                self.search_specials,
            )
            self.logger.debug(
                "Script Config:  SearchByYear=%s",
                self.search_by_year,
            )
            self.logger.debug(
                "Script Config:  SearchInReverse=%s",
                self.search_in_reverse,
            )
            self.logger.debug(
                "Script Config:  CommandLimit=%s",
                self.search_command_limit,
            )
            self.logger.debug(
                "Script Config:  DatabaseFile=%s",
                self.arr_db_file,
            )
            self.logger.debug(
                "Script Config:  MaximumDeletablePercentage=%s",
                self.maximum_deletable_percentage,
            )
            self.logger.debug(
                "Script Config:  DoUpgradeSearch=%s",
                self.do_upgrade_search,
            )
            self.logger.debug(
                "Script Config:  PrioritizeTodaysReleases=%s",
                self.prioritize_todays_release,
            )
            self.logger.debug(
                "Script Config:  SearchBySeries=%s",
                self.series_search,
            )
            self.logger.debug(
                "Script Config:  SearchOmbiRequests=%s",
                self.ombi_search_requests,
            )
            if self.ombi_search_requests:
                self.logger.debug(
                    "Script Config:  OmbiURI=%s",
                    self.ombi_uri,
                )
                self.logger.debug(
                    "Script Config:  OmbiAPIKey=%s",
                    self.ombi_api_key,
                )
                self.logger.debug(
                    "Script Config:  ApprovedOnly=%s",
                    self.ombi_approved_only,
                )
            self.logger.debug(
                "Script Config:  SearchOverseerrRequests=%s",
                self.overseerr_requests,
            )
            if self.overseerr_requests:
                self.logger.debug(
                    "Script Config:  OverseerrURI=%s",
                    self.overseerr_uri,
                )
                self.logger.debug(
                    "Script Config:  OverseerrAPIKey=%s",
                    self.overseerr_api_key,
                )
            if self.ombi_search_requests or self.overseerr_requests:
                self.logger.debug(
                    "Script Config:  SearchRequestsEvery=%s",
                    self.search_requests_every_x_seconds,
                )

            if self.type == "sonarr":
                if self.quality_unmet_search or self.do_upgrade_search:
                    self.search_api_command = "SeriesSearch"
                else:
                    self.search_api_command = "MissingEpisodeSearch"

        self.search_setup_completed = False
        self.model_arr_file: EpisodesModel | MoviesModel | MoviesModelv5 = None
        self.model_arr_series_file: SeriesModel | SeriesModelv4 = None
        self.model_arr_movies_file: MoviesMetadataModel = None

        self.model_arr_command: CommandsModel = None
        self.model_file: EpisodeFilesModel | MoviesFilesModel = None
        self.series_file_model: SeriesFilesModel = None
        self.model_queue: EpisodeQueueModel | MovieQueueModel = None
        self.persistent_queue: FilesQueued = None
        self.logger.hnotice("Starting %s monitor", self._name)

    @property
    def is_alive(self) -> bool:
        try:
            if 1 in self.expiring_bool:
                return True
            if self.session is None:
                self.expiring_bool.add(1)
                return True
            req = self.session.get(
                f"{self.uri}/api/v3/system/status", timeout=10, params={"apikey": self.apikey}
            )
            req.raise_for_status()
            self.logger.trace("Successfully connected to %s", self.uri)
            self.expiring_bool.add(1)
            return True
        except requests.HTTPError:
            self.expiring_bool.add(1)
            return True
        except requests.RequestException:
            self.logger.warning("Could not connect to %s", self.uri)
        return False

    @staticmethod
    def is_ignored_state(torrent: TorrentDictionary) -> bool:
        return torrent.state_enum in (
            TorrentStates.FORCED_DOWNLOAD,
            TorrentStates.FORCED_UPLOAD,
            TorrentStates.CHECKING_UPLOAD,
            TorrentStates.CHECKING_DOWNLOAD,
            TorrentStates.CHECKING_RESUME_DATA,
            TorrentStates.ALLOCATING,
            TorrentStates.MOVING,
            TorrentStates.QUEUED_DOWNLOAD,
        )

    @staticmethod
    def is_uploading_state(torrent: TorrentDictionary) -> bool:
        return torrent.state_enum in (
            TorrentStates.UPLOADING,
            TorrentStates.STALLED_UPLOAD,
            TorrentStates.QUEUED_UPLOAD,
        )

    @staticmethod
    def is_complete_state(torrent: TorrentDictionary) -> bool:
        """Returns True if the State is categorized as Complete."""
        return torrent.state_enum in (
            TorrentStates.UPLOADING,
            TorrentStates.STALLED_UPLOAD,
            TorrentStates.PAUSED_UPLOAD,
            TorrentStates.QUEUED_UPLOAD,
        )

    @staticmethod
    def is_downloading_state(torrent: TorrentDictionary) -> bool:
        """Returns True if the State is categorized as Downloading."""
        return torrent.state_enum in (
            TorrentStates.DOWNLOADING,
            TorrentStates.PAUSED_DOWNLOAD,
        )

    def _get_arr_modes(
        self,
    ) -> tuple[
        type[EpisodesModel] | type[MoviesModel] | type[MoviesModelv5],
        type[CommandsModel],
        type[SeriesModel] | type[SeriesModelv4] | type[MoviesMetadataModel],
    ]:  # sourcery skip: replace-interpolation-with-fstring, switch
        if self.type == "sonarr":
            if self.version.major == 3:
                return EpisodesModel, CommandsModel, SeriesModel
            elif self.version.major == 4:
                return EpisodesModel, CommandsModel, SeriesModelv4
        elif self.type == "radarr":
            if self.version.major == 4:
                return MoviesModel, CommandsModel, MoviesMetadataModel
            elif self.version.major == 5:
                return MoviesModelv5, CommandsModel, MoviesMetadataModel
        else:
            raise UnhandledError("Well you shouldn't have reached here, Arr.type=%s" % self.type)

    def _get_models(
        self,
    ) -> tuple[
        type[EpisodeFilesModel] | type[MoviesFilesModel],
        type[EpisodeQueueModel] | type[MovieQueueModel],
        type[SeriesFilesModel] | None,
    ]:
        if self.type == "sonarr":
            if self.series_search:
                return EpisodeFilesModel, EpisodeQueueModel, SeriesFilesModel
            return EpisodeFilesModel, EpisodeQueueModel, None
        elif self.type == "radarr":
            return MoviesFilesModel, MovieQueueModel, None
        else:
            raise UnhandledError(f"Well you shouldn't have reached here, Arr.type={self.type}")

    def _get_oversee_requests_all(self) -> dict[str, set]:
        try:
            key = "approved" if self.overseerr_approved_only else "unavailable"
            data = defaultdict(set)
            response = self.session.get(
                url=f"{self.overseerr_uri}/api/v1/request",
                headers={"X-Api-Key": self.overseerr_api_key},
                params={"take": 100, "skip": 0, "sort": "added", "filter": key},
                timeout=2,
            )
            response = response.json().get("results", [])
            type_ = None
            if self.type == "radarr":
                type_ = "movie"
            elif self.type == "sonarr":
                type_ = "tv"
            _now = datetime.now()
            for entry in response:
                type__ = entry.get("type")
                if type__ == "movie":
                    id__ = entry.get("media", {}).get("tmdbId")
                elif type__ == "tv":
                    id__ = entry.get("media", {}).get("tvdbId")
                if type_ != type__:
                    continue
                if self.overseerr_is_4k and entry.get("is4k"):
                    if self.overseerr_approved_only:
                        if entry.get("media", {}).get("status4k") != 3:
                            continue
                    elif entry.get("media", {}).get("status4k") == 5:
                        continue
                elif not self.overseerr_is_4k and not entry.get("is4k"):
                    if self.overseerr_approved_only:
                        if entry.get("media", {}).get("status") != 3:
                            continue
                    elif entry.get("media", {}).get("status") == 5:
                        continue
                else:
                    continue
                if id__ in self.overseerr_requests_release_cache:
                    date = self.overseerr_requests_release_cache[id__]
                else:
                    date = datetime(day=1, month=1, year=1970)
                    date_string_backup = f"{_now.year}-{_now.month:02}-{_now.day:02}"
                    date_string = None
                    try:
                        if type_ == "movie":
                            _entry_data = self.session.get(
                                url=f"{self.overseerr_uri}/api/v1/movies/{id__}",
                                headers={"X-Api-Key": self.overseerr_api_key},
                                timeout=2,
                            )
                            date_string = _entry_data.json().get("releaseDate")
                        elif type__ == "tv":
                            _entry_data = self.session.get(
                                url=f"{self.overseerr_uri}/api/v1/tv/{id__}",
                                headers={"X-Api-Key": self.overseerr_api_key},
                                timeout=2,
                            )
                            # We don't do granular (episode/season) searched here so no need to
                            # suppose them
                            date_string = _entry_data.json().get("firstAirDate")
                        if not date_string:
                            date_string = date_string_backup
                        date = datetime.strptime(date_string, "%Y-%m-%d")
                        if date > _now:
                            continue
                        self.overseerr_requests_release_cache[id__] = date
                    except Exception as e:
                        self.logger.warning("Failed to query release date from Overseerr: %s", e)
                if media := entry.get("media"):
                    if imdbId := media.get("imdbId"):
                        data["ImdbId"].add(imdbId)
                    if self.type == "sonarr" and (tvdbId := media.get("tvdbId")):
                        data["TvdbId"].add(tvdbId)
                    elif self.type == "radarr" and (tmdbId := media.get("tmdbId")):
                        data["TmdbId"].add(tmdbId)
            self._temp_overseer_request_cache = data
        except requests.exceptions.ConnectionError:
            self.logger.warning("Couldn't connect to Overseerr")
            self._temp_overseer_request_cache = defaultdict(set)
            return self._temp_overseer_request_cache
        except requests.exceptions.ReadTimeout:
            self.logger.warning("Connection to Overseerr timed out")
            self._temp_overseer_request_cache = defaultdict(set)
            return self._temp_overseer_request_cache
        except Exception as e:
            self.logger.exception(e, exc_info=sys.exc_info())
            self._temp_overseer_request_cache = defaultdict(set)
            return self._temp_overseer_request_cache
        else:
            return self._temp_overseer_request_cache

    def _get_overseerr_requests_count(self) -> int:
        self._get_oversee_requests_all()
        if self.type == "sonarr":
            return len(
                self._temp_overseer_request_cache.get("TvdbId", [])
                or self._temp_overseer_request_cache.get("ImdbId", [])
            )
        elif self.type == "radarr":
            return len(
                self._temp_overseer_request_cache.get("ImdbId", [])
                or self._temp_overseer_request_cache.get("TmdbId", [])
            )
        return 0

    def _get_ombi_request_count(self) -> int:
        if self.type == "sonarr":
            extras = "/api/v1/Request/tv/total"
        elif self.type == "radarr":
            extras = "/api/v1/Request/movie/total"
        else:
            raise UnhandledError(f"Well you shouldn't have reached here, Arr.type={self.type}")
        try:
            response = self.session.get(
                url=f"{self.ombi_uri}{extras}", headers={"ApiKey": self.ombi_api_key}
            )
        except Exception as e:
            self.logger.exception(e, exc_info=sys.exc_info())
            return 0
        else:
            return response.json()

    def _get_ombi_requests(self) -> list[dict]:
        if self.type == "sonarr":
            extras = "/api/v1/Request/tvlite"
        elif self.type == "radarr":
            extras = "/api/v1/Request/movie"
        else:
            raise UnhandledError(f"Well you shouldn't have reached here, Arr.type={self.type}")
        try:
            response = self.session.get(
                url=f"{self.ombi_uri}{extras}", headers={"ApiKey": self.ombi_api_key}
            )
            return response.json()
        except Exception as e:
            self.logger.exception(e, exc_info=sys.exc_info())
            return []

    def _process_ombi_requests(self) -> dict[str, set[str, int]]:
        requests = self._get_ombi_requests()
        data = defaultdict(set)
        for request in requests:
            if self.type == "radarr" and self.ombi_approved_only and request.get("denied") is True:
                continue
            elif self.type == "sonarr" and self.ombi_approved_only:
                # This is me being lazy and not wanting to deal with partially approved requests.
                if any(child.get("denied") is True for child in request.get("childRequests", [])):
                    continue
            if imdbId := request.get("imdbId"):
                data["ImdbId"].add(imdbId)
            if self.type == "radarr" and (theMovieDbId := request.get("theMovieDbId")):
                data["TmdbId"].add(theMovieDbId)
            if self.type == "sonarr" and (tvDbId := request.get("tvDbId")):
                data["TvdbId"].add(tvDbId)
        return data

    def _process_paused(self) -> None:
        # Bulks pause all torrents flagged for pausing.
        if self.pause:
            self.needs_cleanup = True
            self.logger.debug("Pausing %s completed torrents", len(self.pause))
            for i in self.pause:
                self.logger.debug(
                    "Pausing %s (%s)",
                    i,
                    self.manager.qbit_manager.name_cache.get(i),
                )
            self.manager.qbit.torrents_pause(torrent_hashes=self.pause)
            self.pause.clear()

    def _process_imports(self) -> None:
        if self.import_torrents:
            self.needs_cleanup = True
            for torrent in self.import_torrents:
                if torrent.hash in self.sent_to_scan:
                    continue
                path = validate_and_return_torrent_file(torrent.content_path)
                if not path.exists():
                    self.timed_ignore_cache.add(torrent.hash)
                    self.logger.warning(
                        "Missing Torrent: [%s] %s (%s) - File does not seem to exist: %s",
                        torrent.state_enum,
                        torrent.name,
                        torrent.hash,
                        path,
                    )
                    continue
                if path in self.sent_to_scan:
                    continue
                self.sent_to_scan_hashes.add(torrent.hash)
                try:
                    if self.type == "sonarr":
                        completed = True
                        while completed:
                            try:
                                completed = False
                                self.client.post_command(
                                    "DownloadedEpisodesScan",
                                    path=str(path),
                                    downloadClientId=torrent.hash.upper(),
                                    importMode=self.import_mode,
                                )
                            except (
                                requests.exceptions.ChunkedEncodingError,
                                requests.exceptions.ContentDecodingError,
                                requests.exceptions.ConnectionError,
                            ):
                                completed = True
                        self.logger.success(
                            "DownloadedEpisodesScan: %s",
                            path,
                        )
                    elif self.type == "radarr":
                        completed = True
                        while completed:
                            try:
                                completed = False
                                self.client.post_command(
                                    "DownloadedMoviesScan",
                                    path=str(path),
                                    downloadClientId=torrent.hash.upper(),
                                    importMode=self.import_mode,
                                )
                            except (
                                requests.exceptions.ChunkedEncodingError,
                                requests.exceptions.ContentDecodingError,
                                requests.exceptions.ConnectionError,
                            ):
                                completed = True
                        self.logger.success(
                            "DownloadedMoviesScan: %s",
                            path,
                        )
                except:
                    self.logger.error(
                        "Downloaded scan error: [%s][%s][%s]",
                        path,
                        torrent.hash.upper(),
                        self.import_mode,
                    )
                self.sent_to_scan.add(path)
            self.import_torrents.clear()

    def _process_failed_individual(self, hash_: str, entry: int, skip_blacklist: set[str]) -> None:
        if hash_ not in skip_blacklist:
            self.logger.debug(
                "Blocklisting: %s (%s)",
                hash_,
                self.manager.qbit_manager.name_cache.get(hash_, "Deleted"),
            )
            self.delete_from_queue(id_=entry, blacklist=True)
        else:
            self.delete_from_queue(id_=entry, blacklist=False)
        if hash_ in self.recently_queue:
            del self.recently_queue[hash_]
        object_id = self.requeue_cache.get(entry)
        if self.re_search and object_id:
            if self.type == "sonarr":
                object_ids = object_id
                for object_id in object_ids:
                    completed = True
                    while completed:
                        try:
                            completed = False
                            data = self.client.get_episode_by_episode_id(object_id)
                            name = data.get("title")
                            series_id = data.get("series", {}).get("id")
                            if name:
                                episodeNumber = data.get("episodeNumber", 0)
                                absoluteEpisodeNumber = data.get("absoluteEpisodeNumber", 0)
                                seasonNumber = data.get("seasonNumber", 0)
                                seriesTitle = data.get("series", {}).get("title")
                                year = data.get("series", {}).get("year", 0)
                                tvdbId = data.get("series", {}).get("tvdbId", 0)
                                self.logger.notice(
                                    "Re-Searching episode: %s (%s) | "
                                    "S%02dE%03d "
                                    "(E%04d) | "
                                    "%s | "
                                    "[tvdbId=%s|id=%s]",
                                    seriesTitle,
                                    year,
                                    seasonNumber,
                                    episodeNumber,
                                    absoluteEpisodeNumber,
                                    name,
                                    tvdbId,
                                    object_id,
                                )
                            else:
                                self.logger.notice(
                                    "Re-Searching episode: %s",
                                    object_id,
                                )
                        except (
                            requests.exceptions.ChunkedEncodingError,
                            requests.exceptions.ContentDecodingError,
                            requests.exceptions.ConnectionError,
                            AttributeError,
                        ):
                            completed = True

                    if object_id in self.queue_file_ids:
                        self.queue_file_ids.remove(object_id)
                    completed = True
                    while completed:
                        try:
                            completed = False
                            self.client.post_command("EpisodeSearch", episodeIds=[object_id])
                        except (
                            requests.exceptions.ChunkedEncodingError,
                            requests.exceptions.ContentDecodingError,
                            requests.exceptions.ConnectionError,
                        ):
                            completed = True
                    if self.persistent_queue and series_id:
                        self.persistent_queue.insert(EntryId=series_id).on_conflict_ignore()
            elif self.type == "radarr":
                completed = True
                while completed:
                    try:
                        completed = False
                        data = self.client.get_movie_by_movie_id(object_id)
                        name = data.get("title")
                        if name:
                            year = data.get("year", 0)
                            tmdbId = data.get("tmdbId", 0)
                            self.logger.notice(
                                "Re-Searching movie: %s (%s) | [tmdbId=%s|id=%s]",
                                name,
                                year,
                                tmdbId,
                                object_id,
                            )
                        else:
                            self.logger.notice(
                                "Re-Searching movie: %s",
                                object_id,
                            )
                    except (
                        requests.exceptions.ChunkedEncodingError,
                        requests.exceptions.ContentDecodingError,
                        requests.exceptions.ConnectionError,
                        AttributeError,
                    ):
                        completed = True
                if object_id in self.queue_file_ids:
                    self.queue_file_ids.remove(object_id)
                completed = True
                while completed:
                    try:
                        completed = False
                        self.client.post_command("MoviesSearch", movieIds=[object_id])
                    except (
                        requests.exceptions.ChunkedEncodingError,
                        requests.exceptions.ContentDecodingError,
                        requests.exceptions.ConnectionError,
                    ):
                        completed = True
                if self.persistent_queue:
                    self.persistent_queue.insert(EntryId=object_id).on_conflict_ignore()

    def _process_errored(self) -> None:
        # Recheck all torrents marked for rechecking.
        if self.recheck:
            self.needs_cleanup = True
            updated_recheck = [r for r in self.recheck]
            self.manager.qbit.torrents_recheck(torrent_hashes=updated_recheck)
            for k in updated_recheck:
                self.timed_ignore_cache.add(k)
            self.recheck.clear()

    def _process_failed(self) -> None:
        to_delete_all = self.delete.union(
            self.missing_files_post_delete, self.downloads_with_bad_error_message_blocklist
        )
        if self.missing_files_post_delete or self.downloads_with_bad_error_message_blocklist:
            delete_ = True
        else:
            delete_ = False
        skip_blacklist = {
            i.upper() for i in self.skip_blacklist.union(self.missing_files_post_delete)
        }
        if to_delete_all:
            self.needs_cleanup = True
            payload = self.process_entries(to_delete_all)
            if payload:
                for entry, hash_ in payload:
                    self._process_failed_individual(
                        hash_=hash_, entry=entry, skip_blacklist=skip_blacklist
                    )
        if self.remove_from_qbit or self.skip_blacklist or to_delete_all:
            # Remove all bad torrents from the Client.
            temp_to_delete = set()
            if to_delete_all:
                self.manager.qbit.torrents_delete(hashes=to_delete_all, delete_files=True)
            if self.remove_from_qbit or self.skip_blacklist:
                temp_to_delete = self.remove_from_qbit.union(self.skip_blacklist)
                self.manager.qbit.torrents_delete(hashes=temp_to_delete, delete_files=True)

            to_delete_all = to_delete_all.union(temp_to_delete)
            for h in to_delete_all:
                self.cleaned_torrents.discard(h)
                self.sent_to_scan_hashes.discard(h)
                if h in self.manager.qbit_manager.name_cache:
                    del self.manager.qbit_manager.name_cache[h]
                if h in self.manager.qbit_manager.cache:
                    del self.manager.qbit_manager.cache[h]
        if delete_:
            self.missing_files_post_delete.clear()
            self.downloads_with_bad_error_message_blocklist.clear()
        self.skip_blacklist.clear()
        self.remove_from_qbit.clear()
        self.delete.clear()

    def _process_file_priority(self) -> None:
        # Set all files marked as "Do not download" to not download.
        for hash_, files in self.change_priority.copy().items():
            self.needs_cleanup = True
            name = self.manager.qbit_manager.name_cache.get(hash_)
            if name:
                self.logger.debug(
                    "Updating file priority on torrent: %s (%s)",
                    name,
                    hash_,
                )
                self.manager.qbit.torrents_file_priority(
                    torrent_hash=hash_, file_ids=files, priority=0
                )
            else:
                self.logger.error("Torrent does not exist? %s", hash_)
            del self.change_priority[hash_]

    def _process_resume(self) -> None:
        if self.resume:
            self.needs_cleanup = True
            self.manager.qbit.torrents_resume(torrent_hashes=self.resume)
            for k in self.resume:
                self.timed_ignore_cache.add(k)
            self.resume.clear()

    def _remove_empty_folders(self) -> None:
        new_sent_to_scan = set()
        if not self.completed_folder.exists():
            return
        for path in absolute_file_paths(self.completed_folder):
            if path.is_dir() and not len(list(absolute_file_paths(path))):
                with contextlib.suppress(FileNotFoundError):
                    path.rmdir()
                self.logger.trace("Removing empty folder: %s", path)
                if path in self.sent_to_scan:
                    self.sent_to_scan.discard(path)
                else:
                    new_sent_to_scan.add(path)
        self.sent_to_scan = new_sent_to_scan
        if not len(list(absolute_file_paths(self.completed_folder))):
            self.sent_to_scan = set()
            self.sent_to_scan_hashes = set()

    def api_calls(self) -> None:
        if not self.is_alive:
            raise NoConnectionrException(
                f"Service: {self._name} did not respond on {self.uri}", type="arr"
            )
        now = datetime.now()
        if (
            self.rss_sync_timer_last_checked is not None
            and self.rss_sync_timer_last_checked < now - timedelta(minutes=self.rss_sync_timer)
        ):
            completed = True
            while completed:
                try:
                    completed = False
                    self.client.post_command("RssSync")
                except (
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError,
                    requests.exceptions.ConnectionError,
                ):
                    completed = True
            self.rss_sync_timer_last_checked = now

        if (
            self.refresh_downloads_timer_last_checked is not None
            and self.refresh_downloads_timer_last_checked
            < now - timedelta(minutes=self.refresh_downloads_timer)
        ):
            completed = True
            while completed:
                try:
                    completed = False
                    self.client.post_command("RefreshMonitoredDownloads")
                except (
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError,
                    requests.exceptions.ConnectionError,
                ):
                    completed = True
            self.refresh_downloads_timer_last_checked = now

    def arr_db_query_commands_count(self) -> int:
        if not self.search_missing:
            return 0
        try:
            search_commands = (  # ilovemywife
                self.model_arr_command.select()
                .where(
                    self.model_arr_command.EndedAt.is_null(True)
                    & self.model_arr_command.Name.endswith("Search")
                    & ~(self.model_arr_command.Name.contains("Missing"))
                )
                .count()
            )
        except peewee.DatabaseError:
            self.logger.trace("No unended commands found")
            search_commands = 0

        return search_commands

    def _search_todays(self, condition):
        if self.prioritize_todays_release:
            condition_today = copy(condition)
            condition_today &= self.model_file.AirDateUtc >= datetime.now(timezone.utc).date()
            for entry in (
                self.model_file.select()
                .where(condition_today)
                .order_by(
                    self.model_file.SeriesTitle,
                    self.model_file.SeasonNumber.desc(),
                    self.model_file.AirDateUtc.desc(),
                )
                .execute()
            ):
                yield entry, True, True
        else:
            yield None, None, None

    def db_get_files(
        self,
    ) -> Iterable[
        tuple[MoviesFilesModel | EpisodeFilesModel | SeriesFilesModel, bool, bool, bool]
    ]:
        if self.type == "sonarr" and self.series_search:
            for i1, i2, i3 in self.db_get_files_series():
                self.logger.trace("Yielding %s", i1.Title)
                yield i1, i2, i3, i3 is not True
        elif self.type == "sonarr" and not self.series_search:
            for i1, i2, i3 in self.db_get_files_episodes():
                yield i1, i2, i3, False
        elif self.type == "radarr":
            for i1, i2, i3 in self.db_get_files_movies():
                yield i1, i2, i3, False

    def db_maybe_reset_entry_searched_state(self):
        if self.type == "sonarr":
            self.db_reset__series_searched_state()
            self.db_reset__episode_searched_state()
        elif self.type == "radarr":
            self.db_reset__movie_searched_state()
        self.loop_completed = False

    def db_reset__series_searched_state(self):
        if self.version.major == 3:
            self.model_arr_series_file: SeriesModel
        elif self.version.major == 4:
            self.model_arr_series_file: SeriesModelv4
        self.series_file_model: SeriesFilesModel
        self.model_file: EpisodeFilesModel
        if (
            self.loop_completed and self.reset_on_completion and self.series_search
        ):  # Only wipe if a loop completed was tagged
            self.series_file_model.update(Searched=False, Upgrade=False).where(
                self.series_file_model.Searched == True
            ).execute()
            try:
                Ids = [id.Id for id in self.model_arr_series_file.select().execute()]
                self.series_file_model.delete().where(
                    self.series_file_model.EntryId.not_in(Ids)
                ).execute()
            except peewee.DatabaseError:
                self.logger.error("Database error")

    def db_reset__episode_searched_state(self):
        self.model_file: EpisodeFilesModel
        if (
            self.loop_completed is True and self.reset_on_completion
        ):  # Only wipe if a loop completed was tagged
            self.model_file.update(Searched=False, Upgrade=False).where(
                self.model_file.Searched == True
            ).execute()
            try:
                Ids = [id.Id for id in self.model_arr_file.select().execute()]
                self.model_file.delete().where(self.model_file.EntryId.not_in(Ids)).execute()
            except peewee.DatabaseError:
                self.logger.error("Database error")

    def db_reset__movie_searched_state(self):
        self.model_file: MoviesFilesModel
        if (
            self.loop_completed is True and self.reset_on_completion
        ):  # Only wipe if a loop completed was tagged
            self.model_file.update(Searched=False, Upgrade=False).where(
                self.model_file.Searched == True
            ).execute()
            try:
                Ids = [id.Id for id in self.model_arr_file.select().execute()]
                self.model_file.delete().where(self.model_file.EntryId.not_in(Ids)).execute()
            except peewee.DatabaseError:
                self.logger.error("Database error")

    def db_get_files_series(
        self,
    ) -> Iterable[tuple[SeriesFilesModel, bool, bool]]:
        if not self.search_missing:
            yield None, False, False
        elif not self.series_search:
            yield None, False, False
        elif self.type == "sonarr":
            condition = self.model_file.AirDateUtc.is_null(False)
            if not self.search_specials:
                condition &= self.model_file.SeasonNumber != 0
            if not self.do_upgrade_search:
                condition &= self.model_file.Searched == False
                condition &= self.model_file.EpisodeFileId == 0
            else:
                condition &= self.model_file.Upgrade == False
            condition &= self.model_file.AirDateUtc < (
                datetime.now(timezone.utc) - timedelta(hours=2)
            )
            condition &= self.model_file.AbsoluteEpisodeNumber.is_null(
                False
            ) | self.model_file.SceneAbsoluteEpisodeNumber.is_null(False)
            for i1, i2, i3 in self._search_todays(condition):
                if i1 is not None:
                    self.logger.trace("Yielding %s", i1.Title)
                    yield i1, i2, i3
            if not self.do_upgrade_search:
                condition = self.series_file_model.Searched == False
            else:
                condition = self.series_file_model.Upgrade == False
            for entry_ in (
                self.series_file_model.select()
                .where(condition)
                .order_by(self.series_file_model.EntryId.asc())
                .execute()
            ):
                self.logger.trace("Yielding %s", entry_.Title)
                yield entry_, False, False

    def db_get_files_episodes(
        self,
    ) -> Iterable[tuple[EpisodeFilesModel, bool, bool]]:
        if not self.search_missing:
            yield None, False, False
        elif self.type == "sonarr":
            condition = self.model_file.AirDateUtc.is_null(False)
            if not self.search_specials:
                condition &= self.model_file.SeasonNumber != 0
            condition &= self.model_file.AirDateUtc.is_null(False)
            if not self.do_upgrade_search:
                if self.quality_unmet_search:
                    condition &= self.model_file.QualityMet == False
                else:
                    condition &= self.model_file.Searched == False
                    condition &= self.model_file.EpisodeFileId == 0
            else:
                condition &= self.model_file.Upgrade == False
            condition &= self.model_file.AirDateUtc < (
                datetime.now(timezone.utc) - timedelta(hours=2)
            )
            condition &= self.model_file.AbsoluteEpisodeNumber.is_null(
                False
            ) | self.model_file.SceneAbsoluteEpisodeNumber.is_null(False)
            today_condition = copy(condition)
            for entry_ in (
                self.model_file.select()
                .where(condition)
                .order_by(
                    self.model_file.SeriesTitle,
                    self.model_file.SeasonNumber.desc(),
                    self.model_file.AirDateUtc.desc(),
                )
                .group_by(self.model_file.SeriesId)
                .execute()
            ):
                condition_series = copy(condition)
                condition_series &= self.model_file.SeriesId == entry_.SeriesId
                has_been_queried = (
                    self.persistent_queue.get_or_none(
                        self.persistent_queue.EntryId == entry_.SeriesId
                    )
                    is not None
                )
                for entry in (
                    self.model_file.select()
                    .where(condition_series)
                    .order_by(
                        self.model_file.SeasonNumber.desc(),
                        self.model_file.AirDateUtc.desc(),
                    )
                    .execute()
                ):
                    yield entry, False, has_been_queried
                    has_been_queried = True
                for i1, i2, i3 in self._search_todays(today_condition):
                    if i1 is not None:
                        yield i1, i2, i3

    def db_get_files_movies(
        self,
    ) -> Iterable[tuple[MoviesFilesModel, bool, bool]]:
        if not self.search_missing:
            yield None, False, False
        if self.type == "radarr":
            condition = self.model_file.Year.is_null(False)
            if self.search_by_year:
                condition &= self.model_file.Year == self.search_current_year
                if not self.do_upgrade_search:
                    if self.quality_unmet_search:
                        condition &= self.model_file.QualityMet == False
                    else:
                        condition &= self.model_file.MovieFileId == 0
                        condition &= self.model_file.Searched == False
                else:
                    condition &= self.model_file.Upgrade == False
            else:
                if not self.do_upgrade_search:
                    if self.quality_unmet_search:
                        condition &= self.model_file.QualityMet == False
                    else:
                        condition &= self.model_file.MovieFileId == 0
                        condition &= self.model_file.Searched == False
                else:
                    condition &= self.model_file.Upgrade == False
            for entry in (
                self.model_file.select()
                .where(condition)
                .order_by(self.model_file.Title.asc())
                .execute()
            ):
                yield entry, False, False

    def db_get_request_files(self) -> Iterable[MoviesFilesModel | EpisodeFilesModel]:
        if (not self.ombi_search_requests) or (not self.overseerr_requests):
            yield None
        if not self.search_missing:
            yield None
        elif self.type == "sonarr":
            condition = self.model_file.IsRequest == True
            if not self.do_upgrade_search:
                if self.quality_unmet_search:
                    condition &= self.model_file.QualityMet == False
                else:
                    condition &= self.model_file.EpisodeFileId == 0
            else:
                condition &= self.model_file.Upgrade == False
            if not self.search_specials:
                condition &= self.model_file.SeasonNumber != 0
            condition &= self.model_file.AbsoluteEpisodeNumber.is_null(
                False
            ) | self.model_file.SceneAbsoluteEpisodeNumber.is_null(False)
            condition &= self.model_file.AirDateUtc.is_null(False)
            condition &= self.model_file.AirDateUtc < (
                datetime.now(timezone.utc) - timedelta(hours=2)
            )
            yield from (
                self.model_file.select()
                .where(condition)
                .order_by(
                    self.model_file.SeriesTitle,
                    self.model_file.SeasonNumber.desc(),
                    self.model_file.AirDateUtc.desc(),
                )
                .execute()
            )
        elif self.type == "radarr":
            condition = self.model_file.Year <= datetime.now().year
            condition &= self.model_file.Year > 0
            if not self.do_upgrade_search:
                if self.quality_unmet_search:
                    condition &= self.model_file.QualityMet == False
                else:
                    condition &= self.model_file.MovieFileId == 0
                    condition &= self.model_file.IsRequest == True
            else:
                condition &= self.model_file.Upgrade == False
            yield from (
                self.model_file.select()
                .where(condition)
                .order_by(self.model_file.Title.asc())
                .execute()
            )

    def db_request_update(self):
        if self.overseerr_requests:
            self.db_overseerr_update()
        else:
            self.db_ombi_update()

    def _db_request_update(self, request_ids: dict[str, set[int | str]]):
        with self.db.atomic():
            try:
                if self.type == "sonarr" and any(i in request_ids for i in ["ImdbId", "TvdbId"]):
                    self.model_arr_file: EpisodesModel
                    if self.version.major == 3:
                        self.model_arr_series_file: SeriesModel
                    elif self.version.major == 4:
                        self.model_arr_series_file: SeriesModelv4
                    condition = self.model_arr_file.AirDateUtc.is_null(False)
                    if not self.search_specials:
                        condition &= self.model_arr_file.SeasonNumber != 0
                    condition &= self.model_arr_file.AbsoluteEpisodeNumber.is_null(
                        False
                    ) | self.model_arr_file.SceneAbsoluteEpisodeNumber.is_null(False)
                    condition &= self.model_arr_file.AirDateUtc < datetime.now(timezone.utc)
                    imdb_con = None
                    tvdb_con = None
                    if ImdbIds := request_ids.get("ImdbId"):
                        imdb_con = self.model_arr_series_file.ImdbId.in_(ImdbIds)
                    if tvDbIds := request_ids.get("TvdbId"):
                        tvdb_con = self.model_arr_series_file.TvdbId.in_(tvDbIds)
                    if imdb_con and tvdb_con:
                        condition &= imdb_con | tvdb_con
                    elif imdb_con:
                        condition &= imdb_con
                    elif tvdb_con:
                        condition &= tvdb_con
                    for db_entry in (
                        self.model_arr_file.select()
                        .join(
                            self.model_arr_series_file,
                            on=(self.model_arr_file.SeriesId == self.model_arr_series_file.Id),
                            join_type=JOIN.LEFT_OUTER,
                        )
                        .switch(self.model_arr_file)
                        .where(condition)
                        .execute()
                    ):
                        self.db_update_single_series(db_entry=db_entry, request=True)
                elif self.type == "radarr" and any(i in request_ids for i in ["ImdbId", "TmdbId"]):
                    if self.version.major == 4:
                        self.model_arr_file: MoviesModel
                    elif self.version.major == 5:
                        self.model_arr_file: MoviesModelv5
                    self.model_arr_movies_file: MoviesMetadataModel
                    condition = self.model_arr_movies_file.Year <= datetime.now().year

                    tmdb_con = None
                    imdb_con = None
                    if ImdbIds := request_ids.get("ImdbId"):
                        imdb_con = self.model_arr_movies_file.ImdbId.in_(ImdbIds)
                    if TmdbIds := request_ids.get("TmdbId"):
                        tmdb_con = self.model_arr_movies_file.TmdbId.in_(TmdbIds)
                    if tmdb_con and imdb_con:
                        condition &= tmdb_con | imdb_con
                    elif tmdb_con:
                        condition &= tmdb_con
                    elif imdb_con:
                        condition &= imdb_con
                    for db_entry in (
                        self.model_arr_file.select()
                        .join(
                            self.model_arr_movies_file,
                            on=(
                                self.model_arr_file.MovieMetadataId
                                == self.model_arr_movies_file.Id
                            ),
                            join_type=JOIN.LEFT_OUTER,
                        )
                        .switch(self.model_arr_file)
                        .where(condition)
                        .order_by(self.model_arr_file.Added.desc())
                        .execute()
                    ):
                        self.db_update_single_series(db_entry=db_entry, request=True)
            except requests.exceptions.ConnectionError:
                self.logger.error("Connection Error")
                raise DelayLoopException(length=300, type=self._name)

    def db_overseerr_update(self):
        if (not self.search_missing) or (not self.overseerr_requests):
            return
        if self._get_overseerr_requests_count() == 0:
            return
        request_ids = self._temp_overseer_request_cache
        if not any(i in request_ids for i in ["ImdbId", "TmdbId", "TvdbId"]):
            return
        self.logger.notice("Started updating database with Overseerr request entries.")
        self._db_request_update(request_ids)
        self.logger.notice("Finished updating database with Overseerr request entries")

    def db_ombi_update(self):
        if (not self.search_missing) or (not self.ombi_search_requests):
            return
        if self._get_ombi_request_count() == 0:
            return
        request_ids = self._process_ombi_requests()
        if not any(i in request_ids for i in ["ImdbId", "TmdbId", "TvdbId"]):
            return
        self.logger.notice("Started updating database with Ombi request entries.")
        self._db_request_update(request_ids)
        self.logger.notice("Finished updating database with Ombi request entries")

    def db_update_todays_releases(self):
        if not self.prioritize_todays_release:
            return
        with self.db.atomic():
            if self.type == "sonarr":
                try:
                    for series in self.model_arr_file.select().where(
                        (self.model_arr_file.AirDateUtc.is_null(False))
                        & (self.model_arr_file.AirDateUtc < datetime.now(timezone.utc))
                        & (self.model_arr_file.AirDateUtc >= datetime.now(timezone.utc).date())
                        & (
                            self.model_arr_file.AbsoluteEpisodeNumber.is_null(False)
                            | self.model_arr_file.SceneAbsoluteEpisodeNumber.is_null(False)
                        ).execute()
                    ):
                        self.db_update_single_series(db_entry=series)
                except BaseException:
                    self.logger.debug("No episode releases found for today")

    def db_update(self):
        if not self.search_missing:
            return
        self.logger.trace(f"Started updating database")
        self.db_update_todays_releases()
        with self.db.atomic():
            try:
                if self.type == "sonarr":
                    if not self.series_search:
                        self.model_arr_file: EpisodesModel
                        _series = set()
                        if self.search_by_year:
                            series_query = self.model_arr_file.select().where(
                                (self.model_arr_file.AirDateUtc.is_null(False))
                                & (self.model_arr_file.AirDateUtc < datetime.now(timezone.utc))
                                & (
                                    self.model_arr_file.AbsoluteEpisodeNumber.is_null(False)
                                    | self.model_arr_file.SceneAbsoluteEpisodeNumber.is_null(False)
                                )
                                & (
                                    self.model_arr_file.AirDateUtc
                                    >= datetime(month=1, day=1, year=int(self.search_current_year))
                                )
                                & (
                                    self.model_arr_file.AirDateUtc
                                    <= datetime(
                                        month=12, day=31, year=int(self.search_current_year)
                                    )
                                )
                            )
                        else:
                            series_query = self.model_arr_file.select().where(
                                (self.model_arr_file.AirDateUtc.is_null(False))
                                & (self.model_arr_file.AirDateUtc < datetime.now(timezone.utc))
                                & (
                                    self.model_arr_file.AbsoluteEpisodeNumber.is_null(False)
                                    | self.model_arr_file.SceneAbsoluteEpisodeNumber.is_null(False)
                                )
                            )
                        if series_query:
                            for series in series_query:
                                _series.add(series.SeriesId)
                                self.db_update_single_series(db_entry=series)
                            for series in self.model_arr_file.select().where(
                                self.model_arr_file.SeriesId.in_(_series)
                            ):
                                self.db_update_single_series(db_entry=series)
                    else:
                        if self.version.major == 3:
                            self.model_arr_series_file: SeriesModel
                        elif self.version.major == 4:
                            self.model_arr_series_file: SeriesModelv4
                        for series in (
                            self.model_arr_series_file.select()
                            .order_by(self.model_arr_series_file.Added.desc())
                            .execute()
                        ):
                            self.db_update_single_series(db_entry=series, series=True)
                elif self.type == "radarr":
                    if self.version.major == 4:
                        self.model_arr_file: MoviesModel
                    elif self.version.major == 5:
                        self.model_arr_file: MoviesModelv5
                    if self.search_by_year:
                        for movies in (
                            self.model_arr_file.select(self.model_arr_file)
                            .join(
                                self.model_arr_movies_file,
                                on=(
                                    self.model_arr_file.MovieMetadataId
                                    == self.model_arr_movies_file.Id
                                ),
                            )
                            .switch(self.model_arr_file)
                            .where(self.model_arr_movies_file.Year == self.search_current_year)
                            .order_by(self.model_arr_file.Added.desc())
                            .execute()
                        ):
                            self.db_update_single_series(db_entry=movies)

                    else:
                        for movies in (
                            self.model_arr_file.select(self.model_arr_file)
                            .join(
                                self.model_arr_movies_file,
                                on=(
                                    self.model_arr_file.MovieMetadataId
                                    == self.model_arr_movies_file.Id
                                ),
                            )
                            .switch(self.model_arr_file)
                            .order_by(self.model_arr_file.Added.desc())
                            .execute()
                        ):
                            self.db_update_single_series(db_entry=movies)
            except peewee.DatabaseError:
                self.logger.error("Database error")
        self.logger.trace(f"Finished updating database")

    def minimum_availability_check(
        self,
        db_entry: MoviesModel | MoviesModelv5 = None,
        metadata: MoviesMetadataModel = None,
    ) -> bool:
        if metadata.Year > datetime.now().year or metadata.Year == 0:
            self.logger.trace(
                "Skipping %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                metadata.Title,
                db_entry.MinimumAvailability,
                metadata.InCinemas,
                metadata.DigitalRelease,
                metadata.PhysicalRelease,
            )
            return False
        elif metadata.Year < datetime.now().year and metadata.Year != 0:
            self.logger.trace(
                "Grabbing %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                metadata.Title,
                db_entry.MinimumAvailability,
                metadata.InCinemas,
                metadata.DigitalRelease,
                metadata.PhysicalRelease,
            )
            return True
        elif (
            metadata.InCinemas is None
            and metadata.DigitalRelease is None
            and metadata.PhysicalRelease is None
            and db_entry.MinimumAvailability == 3
        ):
            self.logger.trace(
                "Grabbing %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                metadata.Title,
                db_entry.MinimumAvailability,
                metadata.InCinemas,
                metadata.DigitalRelease,
                metadata.PhysicalRelease,
            )
            return True
        elif (
            metadata.DigitalRelease is not None
            and metadata.PhysicalRelease is not None
            and db_entry.MinimumAvailability == 3
        ):
            if (
                datetime.strptime(metadata.DigitalRelease[:19], "%Y-%m-%d %H:%M:%S")
                <= datetime.now()
                or datetime.strptime(metadata.PhysicalRelease[:19], "%Y-%m-%d %H:%M:%S")
                <= datetime.now()
            ):
                self.logger.trace(
                    "Grabbing %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                    metadata.Title,
                    db_entry.MinimumAvailability,
                    metadata.InCinemas,
                    metadata.DigitalRelease,
                    metadata.PhysicalRelease,
                )
                return True
            else:
                self.logger.trace(
                    "Skipping %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                    metadata.Title,
                    db_entry.MinimumAvailability,
                    metadata.InCinemas,
                    metadata.DigitalRelease,
                    metadata.PhysicalRelease,
                )
                return False
        elif (
            metadata.DigitalRelease is not None or metadata.PhysicalRelease is not None
        ) and db_entry.MinimumAvailability == 3:
            if metadata.DigitalRelease is not None:
                if (
                    datetime.strptime(metadata.DigitalRelease[:19], "%Y-%m-%d %H:%M:%S")
                    <= datetime.now()
                ):
                    self.logger.trace(
                        "Grabbing %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                        metadata.Title,
                        db_entry.MinimumAvailability,
                        metadata.InCinemas,
                        metadata.DigitalRelease,
                        metadata.PhysicalRelease,
                    )
                    return True
                else:
                    self.logger.trace(
                        "Skipping %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                        metadata.Title,
                        db_entry.MinimumAvailability,
                        metadata.InCinemas,
                        metadata.DigitalRelease,
                        metadata.PhysicalRelease,
                    )
                    return False
            else:
                if (
                    datetime.strptime(metadata.PhysicalRelease[:19], "%Y-%m-%d %H:%M:%S")
                    <= datetime.now()
                ):
                    self.logger.trace(
                        "Grabbing %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                        metadata.Title,
                        db_entry.MinimumAvailability,
                        metadata.InCinemas,
                        metadata.DigitalRelease,
                        metadata.PhysicalRelease,
                    )
                    return True
                else:
                    self.logger.trace(
                        "Skipping %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                        metadata.Title,
                        db_entry.MinimumAvailability,
                        metadata.InCinemas,
                        metadata.DigitalRelease,
                        metadata.PhysicalRelease,
                    )
                    return False
        elif (
            metadata.InCinemas is None
            and metadata.DigitalRelease is None
            and metadata.PhysicalRelease is None
            and db_entry.MinimumAvailability == 2
        ):
            self.logger.trace(
                "Grabbing %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                metadata.Title,
                db_entry.MinimumAvailability,
                metadata.InCinemas,
                metadata.DigitalRelease,
                metadata.PhysicalRelease,
            )
            return True
        elif metadata.InCinemas is not None and db_entry.MinimumAvailability == 2:
            if datetime.strptime(metadata.InCinemas[:19], "%Y-%m-%d %H:%M:%S") <= datetime.now():
                self.logger.trace(
                    "Grabbing %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                    metadata.Title,
                    db_entry.MinimumAvailability,
                    metadata.InCinemas,
                    metadata.DigitalRelease,
                    metadata.PhysicalRelease,
                )
                return True
            else:
                self.logger.trace(
                    "Skipping %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                    metadata.Title,
                    db_entry.MinimumAvailability,
                    metadata.InCinemas,
                    metadata.DigitalRelease,
                    metadata.PhysicalRelease,
                )
                return False
        elif metadata.InCinemas is None and db_entry.MinimumAvailability == 2:
            if metadata.DigitalRelease is not None:
                if (
                    datetime.strptime(metadata.DigitalRelease[:19], "%Y-%m-%d %H:%M:%S")
                    <= datetime.now()
                ):
                    self.logger.trace(
                        "Grabbing %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                        metadata.Title,
                        db_entry.MinimumAvailability,
                        metadata.InCinemas,
                        metadata.DigitalRelease,
                        metadata.PhysicalRelease,
                    )
                    return True
                else:
                    self.logger.trace(
                        "Skipping %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                        metadata.Title,
                        db_entry.MinimumAvailability,
                        metadata.InCinemas,
                        metadata.DigitalRelease,
                        metadata.PhysicalRelease,
                    )
                    return False
            elif metadata.PhysicalRelease is not None:
                if (
                    datetime.strptime(metadata.DigitalRelease[:19], "%Y-%m-%d %H:%M:%S")
                    <= datetime.now()
                ):
                    self.logger.trace(
                        "Grabbing %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                        metadata.Title,
                        db_entry.MinimumAvailability,
                        metadata.InCinemas,
                        metadata.DigitalRelease,
                        metadata.PhysicalRelease,
                    )
                    return True
                else:
                    self.logger.trace(
                        "Skipping %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                        metadata.Title,
                        db_entry.MinimumAvailability,
                        metadata.InCinemas,
                        metadata.DigitalRelease,
                        metadata.PhysicalRelease,
                    )
                    return False
            else:
                self.logger.trace(
                    "Skipping %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                    metadata.Title,
                    db_entry.MinimumAvailability,
                    metadata.InCinemas,
                    metadata.DigitalRelease,
                    metadata.PhysicalRelease,
                )
                return False
        elif db_entry.MinimumAvailability == 1:
            self.logger.trace(
                "Grabbing %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                metadata.Title,
                db_entry.MinimumAvailability,
                metadata.InCinemas,
                metadata.DigitalRelease,
                metadata.PhysicalRelease,
            )
            return True
        else:
            self.logger.trace(
                "Skipping %s - Minimum Availability: %s, Dates Cinema:%s, Digital:%s, Physical:%s",
                metadata.Title,
                db_entry.MinimumAvailability,
                metadata.InCinemas,
                metadata.DigitalRelease,
                metadata.PhysicalRelease,
            )
            return False

    def db_update_single_series(
        self,
        db_entry: EpisodesModel | SeriesModel | SeriesModelv4 | MoviesModel | MoviesModelv5 = None,
        request: bool = False,
        series: bool = False,
    ):
        if self.search_missing is False:
            return
        try:
            searched = False
            if self.type == "sonarr":
                if not series:
                    db_entry: EpisodesModel
                    self.model_file: EpisodeFilesModel

                    completed = True
                    while completed:
                        try:
                            completed = False
                            EpisodeMetadata = self.client.get_episode_by_episode_id(db_entry.Id)
                        except (
                            requests.exceptions.ChunkedEncodingError,
                            requests.exceptions.ContentDecodingError,
                            requests.exceptions.ConnectionError,
                        ):
                            completed = True

                    QualityUnmet = EpisodeMetadata.get("qualityCutoffNotMet", False)
                    if db_entry.EpisodeFileId != 0 and not self.quality_unmet_search:
                        searched = True
                        self.model_queue.update(Completed=True).where(
                            self.model_queue.EntryId == db_entry.Id
                        ).execute()

                    if db_entry.Monitored == True:
                        EntryId = db_entry.Id

                        SeriesTitle = EpisodeMetadata.get("series", {}).get("title")
                        SeasonNumber = db_entry.SeasonNumber
                        Title = db_entry.Title
                        SeriesId = db_entry.SeriesId
                        EpisodeFileId = db_entry.EpisodeFileId
                        EpisodeNumber = db_entry.EpisodeNumber
                        AbsoluteEpisodeNumber = db_entry.AbsoluteEpisodeNumber
                        SceneAbsoluteEpisodeNumber = db_entry.SceneAbsoluteEpisodeNumber
                        LastSearchTime = db_entry.LastSearchTime
                        AirDateUtc = db_entry.AirDateUtc
                        Monitored = db_entry.Monitored
                        searched = searched
                        QualityMet = QualityUnmet

                        if self.quality_unmet_search and QualityMet:
                            self.logger.trace(
                                "Quality Met | %s | S%02dE%03d",
                                SeriesTitle,
                                SeasonNumber,
                                EpisodeNumber,
                            )

                        to_update = {
                            self.model_file.Monitored: Monitored,
                            self.model_file.Title: Title,
                            self.model_file.AirDateUtc: AirDateUtc,
                            self.model_file.LastSearchTime: LastSearchTime,
                            self.model_file.SceneAbsoluteEpisodeNumber: SceneAbsoluteEpisodeNumber,
                            self.model_file.AbsoluteEpisodeNumber: AbsoluteEpisodeNumber,
                            self.model_file.EpisodeNumber: EpisodeNumber,
                            self.model_file.EpisodeFileId: EpisodeFileId,
                            self.model_file.SeriesId: SeriesId,
                            self.model_file.SeriesTitle: SeriesTitle,
                            self.model_file.SeasonNumber: SeasonNumber,
                            self.model_file.QualityMet: QualityMet,
                        }
                        if searched:
                            to_update[self.model_file.Searched] = searched

                        upgrade = False
                        try:
                            if self.model_file.get_or_none(
                                self.model_file.EntryId == EntryId
                            ).Upgrade:
                                upgrade = True
                                to_update[self.model_file.Upgrade] = upgrade
                        except AttributeError:
                            pass

                        self.logger.trace(
                            "Updating database entry | %s | S%02dE%03d [Searched:%s][Upgrade:%s]",
                            SeriesTitle,
                            SeasonNumber,
                            EpisodeNumber,
                            searched,
                            upgrade,
                        )

                        if request:
                            to_update[self.model_file.IsRequest] = request

                        db_commands = self.model_file.insert(
                            EntryId=EntryId,
                            Title=Title,
                            SeriesId=SeriesId,
                            EpisodeFileId=EpisodeFileId,
                            EpisodeNumber=EpisodeNumber,
                            AbsoluteEpisodeNumber=AbsoluteEpisodeNumber,
                            SceneAbsoluteEpisodeNumber=SceneAbsoluteEpisodeNumber,
                            LastSearchTime=LastSearchTime,
                            AirDateUtc=AirDateUtc,
                            Monitored=Monitored,
                            SeriesTitle=SeriesTitle,
                            SeasonNumber=SeasonNumber,
                            Searched=searched,
                            IsRequest=request,
                            QualityMet=QualityMet,
                            Upgrade=upgrade,
                        ).on_conflict(
                            conflict_target=[self.model_file.EntryId],
                            update=to_update,
                        )
                        db_commands.execute()
                    else:
                        return
                else:
                    if self.version.major == 3:
                        db_entry: SeriesModel
                    elif self.version.major == 4:
                        db_entry: SeriesModelv4
                    self.series_file_model: SeriesFilesModel
                    EntryId = db_entry.Id
                    if db_entry.Monitored == True:
                        completed = True
                        while completed:
                            try:
                                completed = False
                                seriesMetadata = self.client.get_series(id_=EntryId)
                            except (
                                requests.exceptions.ChunkedEncodingError,
                                requests.exceptions.ContentDecodingError,
                                requests.exceptions.ConnectionError,
                            ):
                                completed = True
                        episodeCount = 0
                        episodeFileCount = 0
                        totalEpisodeCount = 0
                        monitoredEpisodeCount = 0
                        seasons = seriesMetadata.get("seasons")
                        for season in seasons:
                            sdict = dict(season)
                            if sdict.get("seasonNumber") == 0:
                                statistics = sdict.get("statistics")
                                monitoredEpisodeCount = monitoredEpisodeCount + statistics.get(
                                    "episodeCount"
                                )
                                totalEpisodeCount = totalEpisodeCount + statistics.get(
                                    "totalEpisodeCount"
                                )
                                episodeFileCount = episodeFileCount + statistics.get(
                                    "episodeFileCount"
                                )
                            else:
                                statistics = sdict.get("statistics")
                                episodeCount = episodeCount + statistics.get("episodeCount")
                                totalEpisodeCount = totalEpisodeCount + statistics.get(
                                    "totalEpisodeCount"
                                )
                                episodeFileCount = episodeFileCount + statistics.get(
                                    "episodeFileCount"
                                )
                        if self.search_specials:
                            searched = totalEpisodeCount == episodeFileCount
                        else:
                            searched = (episodeCount + monitoredEpisodeCount) == episodeFileCount
                        Title = seriesMetadata.get("title")
                        Monitored = db_entry.Monitored

                        to_update = {
                            self.series_file_model.Monitored: Monitored,
                            self.series_file_model.Title: Title,
                        }

                        if searched:
                            to_update[self.series_file_model.Searched] = searched

                        upgrade = False
                        try:
                            if self.series_file_model.get_or_none(
                                self.series_file_model.EntryId == EntryId
                            ).Upgrade:
                                upgrade = True
                                to_update[self.series_file_model.Upgrade] = upgrade
                        except AttributeError:
                            pass

                        self.logger.trace(
                            "Updating database entry | %s [Searched:%s][Upgrade:%s]",
                            Title,
                            searched,
                            upgrade,
                        )

                        db_commands = self.series_file_model.insert(
                            EntryId=EntryId,
                            Title=Title,
                            Searched=searched,
                            Monitored=Monitored,
                            Upgrade=upgrade,
                        ).on_conflict(
                            conflict_target=[self.series_file_model.EntryId],
                            update=to_update,
                        )
                        db_commands.execute()
                    else:
                        return

            elif self.type == "radarr":
                self.model_file: MoviesFilesModel
                if self.version.major == 4:
                    db_entry: MoviesModel
                elif self.version.major == 5:
                    db_entry: MoviesModelv5
                searched = False
                completed = True
                while completed:
                    try:
                        completed = False
                        movieData = self.client.get_movie_by_movie_id(db_entry.Id)
                    except (
                        requests.exceptions.ChunkedEncodingError,
                        requests.exceptions.ContentDecodingError,
                        requests.exceptions.ConnectionError,
                        JSONDecodeError,
                    ):
                        completed = True
                QualityUnmet = movieData.get("qualityCutoffNotMet", False)
                if db_entry.MovieFileId != 0 and not self.quality_unmet_search:
                    searched = True
                    self.model_queue.update(Completed=True).where(
                        self.model_queue.EntryId == db_entry.Id
                    ).execute()

                movieMetadata = self.model_arr_movies_file.get(
                    self.model_arr_movies_file.Id == db_entry.MovieMetadataId
                )
                movieMetadata: MoviesMetadataModel

                if (
                    self.minimum_availability_check(db_entry, movieMetadata)
                    and db_entry.Monitored == True
                ):
                    title = movieMetadata.Title
                    monitored = db_entry.Monitored
                    tmdbId = movieMetadata.TmdbId
                    year = movieMetadata.Year
                    entryId = db_entry.Id
                    movieFileId = db_entry.MovieFileId
                    qualityMet = QualityUnmet

                    to_update = {
                        self.model_file.MovieFileId: movieFileId,
                        self.model_file.Monitored: monitored,
                        self.model_file.QualityMet: qualityMet,
                    }

                    if searched:
                        to_update[self.model_file.Searched] = searched

                    upgrade = False
                    try:
                        if self.model_file.get_or_none(self.model_file.EntryId == entryId).Upgrade:
                            upgrade = True
                            to_update[self.model_file.Upgrade] = upgrade
                    except AttributeError:
                        pass

                    if request:
                        to_update[self.model_file.IsRequest] = request

                    self.logger.trace(
                        "Updating database entry | %s [Searched:%s][Upgrade:%s]",
                        title,
                        searched,
                        upgrade,
                    )

                    db_commands = self.model_file.insert(
                        Title=title,
                        Monitored=monitored,
                        TmdbId=tmdbId,
                        Year=year,
                        EntryId=entryId,
                        Searched=searched,
                        MovieFileId=movieFileId,
                        IsRequest=request,
                        QualityMet=qualityMet,
                        Upgrade=upgrade,
                    ).on_conflict(
                        conflict_target=[self.model_file.EntryId],
                        update=to_update,
                    )
                    db_commands.execute()
                else:
                    return

        except requests.exceptions.ConnectionError as e:
            self.logger.debug(
                "Max retries exceeded for %s ID:%s", self._name, db_entry.Id, exc_info=e
            )
            raise DelayLoopException(length=300, type=self._name)
        except JSONDecodeError:
            if self.type == "sonarr":
                self.logger.warning(
                    "Error getting series info: [%s][%s]", db_entry.Id, db_entry.Title
                )
            elif self.type == "radarr":
                self.logger.warning(
                    "Error getting movie info: [%s][%s]", db_entry.Id, db_entry.Path
                )
        except Exception as e:
            self.logger.error(e, exc_info=sys.exc_info())

    def delete_from_queue(self, id_, remove_from_client=True, blacklist=True):
        completed = True
        while completed:
            try:
                completed = False
                res = self.client.del_queue(id_, remove_from_client, blacklist)
            except (
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ContentDecodingError,
                requests.exceptions.ConnectionError,
            ):
                completed = True
        return res

    def file_is_probeable(self, file: pathlib.Path) -> bool:
        if not self.manager.ffprobe_available:
            return True  # ffprobe is not found, so we say every file is acceptable.
        try:
            if file in self.files_probed:
                self.logger.trace("Probeable: File has already been probed: %s", file)
                return True
            if file.is_dir():
                self.logger.trace("Not probeable: File is a directory: %s", file)
                return False
            output = ffmpeg.probe(
                str(file.absolute()), cmd=self.manager.qbit_manager.ffprobe_downloader.probe_path
            )
            if not output:
                self.logger.trace("Not probeable: Probe returned no output: %s", file)
                return False
            self.files_probed.add(file)
            return True
        except ffmpeg.Error as e:
            error = e.stderr.decode()
            self.logger.trace(
                "Not probeable: Probe returned an error: %s:\n%s",
                file,
                e.stderr,
                exc_info=sys.exc_info(),
            )
            if "Invalid data found when processing input" in error:
                return False
            return False

    def folder_cleanup(self, downloads_id: str | None, folder: pathlib.Path):
        if self.auto_delete is False:
            return
        self.logger.debug("Folder Cleanup: %s", folder)
        all_files_in_folder = list(absolute_file_paths(folder))
        invalid_files = set()
        probeable = 0
        for file in all_files_in_folder:
            if file.name in {"desktop.ini", ".DS_Store"}:
                continue
            elif file.suffix.lower() == ".parts":
                continue
            if not file.exists():
                continue
            if file.is_dir():
                self.logger.trace("Folder Cleanup: File is a folder: %s", file)
                continue
            if self.file_extension_allowlist and (
                (match := self.file_extension_allowlist.search(file.suffix)) and match.group()
            ):
                self.logger.trace("Folder Cleanup: File has an allowed extension: %s", file)
                if self.file_is_probeable(file):
                    self.logger.trace("Folder Cleanup: File is a valid media type: %s", file)
                    probeable += 1

            else:
                invalid_files.add(file)

        if not probeable:
            self.downloads_with_bad_error_message_blocklist.discard(downloads_id)
            self.delete.discard(downloads_id)
            self.remove_and_maybe_blocklist(downloads_id, folder)
        elif invalid_files:
            for file in invalid_files:
                self.remove_and_maybe_blocklist(None, file)

    def post_file_cleanup(self):
        for downloads_id, file in self.files_to_cleanup:
            self.folder_cleanup(downloads_id, file)
        self.files_to_cleanup = set()

    def post_download_error_cleanup(self):
        for downloads_id, file in self.files_to_explicitly_delete:
            self.remove_and_maybe_blocklist(downloads_id, file)

    def remove_and_maybe_blocklist(self, downloads_id: str | None, file_or_folder: pathlib.Path):
        if downloads_id is not None:
            self.delete_from_queue(id_=downloads_id, blacklist=True)
            self.logger.debug(
                "Torrent removed and blocklisted: File was marked as failed by Arr " "| %s",
                file_or_folder,
            )

        if file_or_folder.is_dir():
            try:
                shutil.rmtree(file_or_folder, ignore_errors=True)
                self.logger.debug(
                    "Folder removed: Folder was marked as failed by Arr, "
                    "manually removing it | %s",
                    file_or_folder,
                )
            except PermissionError:
                self.logger.debug(
                    "Folder in use: Failed to remove Folder: Folder was marked as failed by Ar "
                    "| %s",
                    file_or_folder,
                )
        else:
            try:
                file_or_folder.unlink(missing_ok=True)
                self.logger.debug(
                    "File removed: File was marked as failed by Arr, " "manually removing it | %s",
                    file_or_folder,
                )
            except PermissionError:
                self.logger.debug(
                    "File in use: Failed to remove file: File was marked as failed by Ar | %s",
                    file_or_folder,
                )

    def all_folder_cleanup(self) -> None:
        if self.auto_delete is False:
            return
        self._update_bad_queue_items()
        self.post_file_cleanup()
        if self.needs_cleanup is False:
            return
        folder = self.completed_folder
        self.folder_cleanup(None, folder)
        self.files_to_explicitly_delete = iter([])
        self.post_download_error_cleanup()
        self._remove_empty_folders()
        self.needs_cleanup = False

    def maybe_do_search(
        self,
        file_model: EpisodeFilesModel | MoviesFilesModel | SeriesFilesModel,
        request: bool = False,
        todays: bool = False,
        bypass_limit: bool = False,
        series_search: bool = False,
    ):
        request_tag = (
            "[OVERSEERR REQUEST]: "
            if request and self.overseerr_requests
            else "[OMBI REQUEST]: "
            if request and self.ombi_search_requests
            else "[PRIORITY SEARCH - TODAY]: "
            if todays
            else ""
        )
        if request or todays:
            bypass_limit = True
        if (not self.search_missing) or (file_model is None):
            return None
        elif not self.is_alive:
            raise NoConnectionrException(f"Could not connect to {self.uri}", type="arr")
        elif self.type == "sonarr":
            if not series_search:
                file_model: EpisodeFilesModel
                if not (request or todays):
                    queue = (
                        self.model_queue.select()
                        .where(self.model_queue.EntryId == file_model.EntryId)
                        .execute()
                    )
                else:
                    queue = False
                if queue:
                    self.logger.debug(
                        "%sSkipping: Already Searched: %s | "
                        "S%02dE%03d | "
                        "%s | [id=%s|AirDateUTC=%s]",
                        request_tag,
                        file_model.SeriesTitle,
                        file_model.SeasonNumber,
                        file_model.EpisodeNumber,
                        file_model.Title,
                        file_model.EntryId,
                        file_model.AirDateUtc,
                    )
                    file_model.update(Searched=True, Upgrade=True).where(
                        file_model.EntryId == file_model.EntryId
                    ).execute()
                    return True
                active_commands = self.arr_db_query_commands_count()
                self.logger.debug(
                    "%s%s active search commands",
                    request_tag,
                    active_commands,
                )
                if not bypass_limit and active_commands >= self.search_command_limit:
                    self.logger.trace(
                        "%sIdle: Too many commands in queue: %s | "
                        "S%02dE%03d | "
                        "%s | [id=%s|AirDateUTC=%s]",
                        request_tag,
                        file_model.SeriesTitle,
                        file_model.SeasonNumber,
                        file_model.EpisodeNumber,
                        file_model.Title,
                        file_model.EntryId,
                        file_model.AirDateUtc,
                    )
                    return False
                self.persistent_queue.insert(
                    EntryId=file_model.SeriesId
                ).on_conflict_ignore().execute()
                self.model_queue.insert(
                    Completed=False,
                    EntryId=file_model.EntryId,
                ).on_conflict_replace().execute()
                if file_model.EntryId not in self.queue_file_ids:
                    completed = True
                    while completed:
                        try:
                            completed = False
                            self.client.post_command(
                                "EpisodeSearch", episodeIds=[file_model.EntryId]
                            )
                        except (
                            requests.exceptions.ChunkedEncodingError,
                            requests.exceptions.ContentDecodingError,
                            requests.exceptions.ConnectionError,
                        ):
                            completed = True
                file_model.update(Searched=True, Upgrade=True).where(
                    file_model.EntryId == file_model.EntryId
                ).execute()
                self.logger.hnotice(
                    "%sSearching for: %s | S%02dE%03d | %s | [id=%s|AirDateUTC=%s]",
                    request_tag,
                    file_model.SeriesTitle,
                    file_model.SeasonNumber,
                    file_model.EpisodeNumber,
                    file_model.Title,
                    file_model.EntryId,
                    file_model.AirDateUtc,
                )
                return True
            else:
                file_model: SeriesFilesModel
                active_commands = self.arr_db_query_commands_count()
                self.logger.debug(
                    "%s%s active search commands",
                    request_tag,
                    active_commands,
                )
                if not bypass_limit and active_commands >= self.search_command_limit:
                    self.logger.trace(
                        "%sIdle: Too many commands in queue: %s | [id=%s]",
                        request_tag,
                        file_model.Title,
                        file_model.EntryId,
                    )
                    return False
                self.persistent_queue.insert(
                    EntryId=file_model.EntryId
                ).on_conflict_ignore().execute()
                self.model_queue.insert(
                    Completed=False,
                    EntryId=file_model.EntryId,
                ).on_conflict_replace().execute()
                completed = True
                while completed:
                    try:
                        completed = False
                        self.client.post_command(
                            self.search_api_command, seriesId=file_model.EntryId
                        )
                    except (
                        requests.exceptions.ChunkedEncodingError,
                        requests.exceptions.ContentDecodingError,
                        requests.exceptions.ConnectionError,
                    ):
                        completed = True
                file_model.update(Searched=True, Upgrade=True).where(
                    file_model.EntryId == file_model.EntryId
                ).execute()
                self.logger.hnotice(
                    "%sSearching for: %s | %s | [id=%s]",
                    request_tag,
                    "Missing episodes in"
                    if "Missing" in self.search_api_command
                    else "All episodes in",
                    file_model.Title,
                    file_model.EntryId,
                )
                return True
        elif self.type == "radarr":
            file_model: MoviesFilesModel
            if not (request or todays) and file_model.EntryId in self.queue_file_ids:
                queue = True
            else:
                queue = False
            if queue:
                self.logger.debug(
                    "%sSkipping: Already Searched: %s (%s)",
                    request_tag,
                    file_model.Title,
                    file_model.EntryId,
                )
                file_model.update(Searched=True, Upgrade=True).where(
                    file_model.EntryId == file_model.EntryId
                ).execute()
                return True
            active_commands = self.arr_db_query_commands_count()
            self.logger.debug(
                "%s%s active search commands",
                request_tag,
                active_commands,
            )
            if not bypass_limit and active_commands >= self.search_command_limit:
                self.logger.trace(
                    "%sIdle: Too many commands in queue: %s | [id=%s]",
                    request_tag,
                    file_model.Title,
                    file_model.EntryId,
                )
                return False
            self.persistent_queue.insert(EntryId=file_model.EntryId).on_conflict_ignore().execute()

            self.model_queue.insert(
                Completed=False,
                EntryId=file_model.EntryId,
            ).on_conflict_replace().execute()
            if file_model.EntryId not in self.queue_file_ids:
                completed = True
                while completed:
                    try:
                        completed = False
                        self.client.post_command("MoviesSearch", movieIds=[file_model.EntryId])
                    except (
                        requests.exceptions.ChunkedEncodingError,
                        requests.exceptions.ContentDecodingError,
                        requests.exceptions.ConnectionError,
                    ):
                        completed = True
            file_model.update(Searched=True, Upgrade=True).where(
                file_model.EntryId == file_model.EntryId
            ).execute()
            self.logger.hnotice(
                "%sSearching for: %s (%s) [tmdbId=%s|id=%s]",
                request_tag,
                file_model.Title,
                file_model.Year,
                file_model.TmdbId,
                file_model.EntryId,
            )
            return True

    def process(self):
        self._process_resume()
        self._process_paused()
        self._process_errored()
        self._process_file_priority()
        self._process_imports()
        self._process_failed()
        self.all_folder_cleanup()

    def process_entries(self, hashes: set[str]) -> tuple[list[tuple[int, str]], set[str]]:
        payload = [
            (_id, h.upper()) for h in hashes if (_id := self.cache.get(h.upper())) is not None
        ]

        return payload

    def process_torrents(self):
        try:
            try:
                torrents = self.manager.qbit_manager.client.torrents.info(
                    status_filter="all", category=self.category, sort="added_on", reverse=False
                )
                torrents = [t for t in torrents if hasattr(t, "category")]
                if not len(torrents):
                    raise DelayLoopException(length=5, type="no_downloads")
                if has_internet() is False:
                    self.manager.qbit_manager.should_delay_torrent_scan = True
                    raise DelayLoopException(length=NO_INTERNET_SLEEP_TIMER, type="internet")
                if self.manager.qbit_manager.should_delay_torrent_scan:
                    raise DelayLoopException(length=NO_INTERNET_SLEEP_TIMER, type="delay")
                self.api_calls()
                self.refresh_download_queue()
                for torrent in torrents:
                    with contextlib.suppress(qbittorrentapi.NotFound404Error):
                        self._process_single_torrent(torrent)
                self.process()
            except NoConnectionrException as e:
                self.logger.error(e.message)
            except requests.exceptions.ConnectionError:
                self.logger.warning("Couldn't connect to %s", self.type)
                self._temp_overseer_request_cache = defaultdict(set)
                return self._temp_overseer_request_cache
            except qbittorrentapi.exceptions.APIError as e:
                exceptionstr = str(e)
                if (
                    exceptionstr.find("JSONDecodeError") != 0
                    or exceptionstr.find("AttributeError") != 0
                ):
                    self.logger.info("Torrent still connecting to trackers")
                else:
                    self.logger.error("The qBittorrent API returned an unexpected error")
                    self.logger.debug("Unexpected APIError from qBitTorrent", exc_info=e)
                    raise DelayLoopException(length=300, type="qbit")
            except (AttributeError, JSONDecodeError):
                self.logger.info("Torrent still connecting to trackers")
            except DelayLoopException:
                raise
            except KeyboardInterrupt:
                self.logger.hnotice("Detected Ctrl+C - Terminating process")
                sys.exit(0)
            except Exception as e:
                self.logger.error(e, exc_info=sys.exc_info())
        except KeyboardInterrupt:
            self.logger.hnotice("Detected Ctrl+C - Terminating process")
            sys.exit(0)
        except DelayLoopException:
            raise

    def _process_single_torrent_failed_cat(self, torrent: qbittorrentapi.TorrentDictionary):
        self.logger.notice(
            "Deleting manually failed torrent: "
            "[Progress: %s%%][Added On: %s]"
            "[Availability: %s%%][Time Left: %s]"
            "[Last active: %s] "
            "| [%s] | %s (%s)",
            round(torrent.progress * 100, 2),
            datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
            round(torrent.availability * 100, 2),
            timedelta(seconds=torrent.eta),
            datetime.fromtimestamp(torrent.last_activity),
            torrent.state_enum,
            torrent.name,
            torrent.hash,
        )
        self.delete.add(torrent.hash)

    def _process_single_torrent_recheck_cat(self, torrent: qbittorrentapi.TorrentDictionary):
        self.logger.notice(
            "Re-checking manually set torrent: "
            "[Progress: %s%%][Added On: %s]"
            "[Availability: %s%%][Time Left: %s]"
            "[Last active: %s] "
            "| [%s] | %s (%s)",
            round(torrent.progress * 100, 2),
            datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
            round(torrent.availability * 100, 2),
            timedelta(seconds=torrent.eta),
            datetime.fromtimestamp(torrent.last_activity),
            torrent.state_enum,
            torrent.name,
            torrent.hash,
        )
        self.recheck.add(torrent.hash)

    def _process_single_torrent_ignored(self, torrent: qbittorrentapi.TorrentDictionary):
        # Do not touch torrents that are currently being ignored.
        self.logger.trace(
            "Skipping torrent: Ignored state | "
            "[Progress: %s%%][Added On: %s]"
            "[Availability: %s%%][Time Left: %s]"
            "[Last active: %s] "
            "| [%s] | %s (%s)",
            round(torrent.progress * 100, 2),
            datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
            round(torrent.availability * 100, 2),
            timedelta(seconds=torrent.eta),
            datetime.fromtimestamp(torrent.last_activity),
            torrent.state_enum,
            torrent.name,
            torrent.hash,
        )
        if torrent.state_enum == TorrentStates.QUEUED_DOWNLOAD:
            self.recently_queue[torrent.hash] = time.time()

    def _process_single_torrent_added_to_ignore_cache(
        self, torrent: qbittorrentapi.TorrentDictionary
    ):
        self.logger.trace(
            "Skipping torrent: Marked for skipping | "
            "[Progress: %s%%][Added On: %s]"
            "[Availability: %s%%][Time Left: %s]"
            "[Last active: %s] "
            "| [%s] | %s (%s)",
            round(torrent.progress * 100, 2),
            datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
            round(torrent.availability * 100, 2),
            timedelta(seconds=torrent.eta),
            datetime.fromtimestamp(torrent.last_activity),
            torrent.state_enum,
            torrent.name,
            torrent.hash,
        )

    def _process_single_torrent_queued_upload(
        self, torrent: qbittorrentapi.TorrentDictionary, leave_alone: bool
    ):
        if leave_alone or torrent.state_enum == TorrentStates.FORCED_UPLOAD:
            self.logger.trace(
                "Torrent State: Queued Upload | Allowing Seeding | "
                "[Progress: %s%%][Added On: %s]"
                "[Availability: %s%%][Time Left: %s]"
                "[Last active: %s] "
                "| [%s] | %s (%s)",
                round(torrent.progress * 100, 2),
                datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
                round(torrent.availability * 100, 2),
                timedelta(seconds=torrent.eta),
                datetime.fromtimestamp(torrent.last_activity),
                torrent.state_enum,
                torrent.name,
                torrent.hash,
            )
        else:
            self.pause.add(torrent.hash)
            self.logger.trace(
                "Pausing torrent: Queued Upload | "
                "[Progress: %s%%][Added On: %s]"
                "[Availability: %s%%][Time Left: %s]"
                "[Last active: %s] "
                "| [%s] | %s (%s)",
                round(torrent.progress * 100, 2),
                datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
                round(torrent.availability * 100, 2),
                timedelta(seconds=torrent.eta),
                datetime.fromtimestamp(torrent.last_activity),
                torrent.state_enum,
                torrent.name,
                torrent.hash,
            )

    def _process_single_torrent_stalled_torrent(
        self, torrent: qbittorrentapi.TorrentDictionary, extra: str
    ):
        # Process torrents who have stalled at this point, only mark for
        # deletion if they have been added more than "IgnoreTorrentsYoungerThan"
        # seconds ago
        if (
            self.recently_queue.get(torrent.hash, torrent.added_on)
            < time.time() - self.ignore_torrents_younger_than
        ):
            self.logger.info(
                "Deleting Stale torrent: %s | "
                "[Progress: %s%%][Added On: %s]"
                "[Availability: %s%%][Time Left: %s]"
                "[Last active: %s] "
                "| [%s] | %s (%s)",
                extra,
                round(torrent.progress * 100, 2),
                datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
                round(torrent.availability * 100, 2),
                timedelta(seconds=torrent.eta),
                datetime.fromtimestamp(torrent.last_activity),
                torrent.state_enum,
                torrent.name,
                torrent.hash,
            )
            self.delete.add(torrent.hash)
        else:
            self.logger.trace(
                "Ignoring Stale torrent: "
                "[Progress: %s%%][Added On: %s]"
                "[Availability: %s%%][Time Left: %s]"
                "[Last active: %s] "
                "| [%s] | %s (%s)",
                round(torrent.progress * 100, 2),
                datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
                round(torrent.availability * 100, 2),
                timedelta(seconds=torrent.eta),
                datetime.fromtimestamp(torrent.last_activity),
                torrent.state_enum,
                torrent.name,
                torrent.hash,
            )

    def _process_single_torrent_percentage_threshold(
        self, torrent: qbittorrentapi.TorrentDictionary, maximum_eta: int
    ):
        # Ignore torrents who have reached maximum percentage as long as
        # the last activity is within the MaximumETA set for this category
        # For example if you set MaximumETA to 5 mines, this will ignore all
        # torrents that have stalled at a higher percentage as long as there is activity
        # And the window of activity is determined by the current time - MaximumETA,
        # if the last active was after this value ignore this torrent
        # the idea here is that if a torrent isn't completely dead some leecher/seeder
        # may contribute towards your progress.
        # However if its completely dead and no activity is observed, then lets
        # remove it and requeue a new torrent.
        if maximum_eta > 0 and torrent.last_activity < (time.time() - maximum_eta):
            self.logger.info(
                "Deleting Stale torrent: Last activity is older than Maximum ETA "
                "[Progress: %s%%][Added On: %s]"
                "[Availability: %s%%][Time Left: %s]"
                "[Last active: %s] "
                "| [%s] | %s (%s)",
                round(torrent.progress * 100, 2),
                datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
                round(torrent.availability * 100, 2),
                timedelta(seconds=torrent.eta),
                datetime.fromtimestamp(torrent.last_activity),
                torrent.state_enum,
                torrent.name,
                torrent.hash,
            )
            self.delete.add(torrent.hash)
        else:
            self.logger.trace(
                "Skipping torrent: Reached Maximum completed "
                "percentage and is active | "
                "[Progress: %s%%][Added On: %s]"
                "[Availability: %s%%][Time Left: %s]"
                "[Last active: %s] "
                "| [%s] | %s (%s)",
                round(torrent.progress * 100, 2),
                datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
                round(torrent.availability * 100, 2),
                timedelta(seconds=torrent.eta),
                datetime.fromtimestamp(torrent.last_activity),
                torrent.state_enum,
                torrent.name,
                torrent.hash,
            )

    def _process_single_torrent_paused(self, torrent: qbittorrentapi.TorrentDictionary):
        self.timed_ignore_cache.add(torrent.hash)
        self.resume.add(torrent.hash)
        self.logger.debug(
            "Resuming incomplete paused torrent: "
            "[Progress: %s%%][Added On: %s]"
            "[Availability: %s%%][Time Left: %s]"
            "[Last active: %s] "
            "| [%s] | %s (%s)",
            round(torrent.progress * 100, 2),
            datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
            round(torrent.availability * 100, 2),
            timedelta(seconds=torrent.eta),
            datetime.fromtimestamp(torrent.last_activity),
            torrent.state_enum,
            torrent.name,
            torrent.hash,
        )

    def _process_single_torrent_already_sent_to_scan(
        self, torrent: qbittorrentapi.TorrentDictionary
    ):
        self.logger.trace(
            "Skipping torrent: Already sent for import | "
            "[Progress: %s%%][Added On: %s]"
            "[Availability: %s%%][Time Left: %s]"
            "[Last active: %s] "
            "| [%s] | %s (%s)",
            round(torrent.progress * 100, 2),
            datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
            round(torrent.availability * 100, 2),
            timedelta(seconds=torrent.eta),
            datetime.fromtimestamp(torrent.last_activity),
            torrent.state_enum,
            torrent.name,
            torrent.hash,
        )

    def _process_single_torrent_errored(self, torrent: qbittorrentapi.TorrentDictionary):
        self.logger.trace(
            "Rechecking Errored torrent: "
            "[Progress: %s%%][Added On: %s]"
            "[Availability: %s%%][Time Left: %s]"
            "[Last active: %s] "
            "| [%s] | %s (%s)",
            round(torrent.progress * 100, 2),
            datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
            round(torrent.availability * 100, 2),
            timedelta(seconds=torrent.eta),
            datetime.fromtimestamp(torrent.last_activity),
            torrent.state_enum,
            torrent.name,
            torrent.hash,
        )
        self.recheck.add(torrent.hash)

    def _process_single_torrent_fully_completed_torrent(
        self, torrent: qbittorrentapi.TorrentDictionary, leave_alone: bool
    ):
        if leave_alone or torrent.state_enum == TorrentStates.FORCED_UPLOAD:
            self.logger.trace(
                "Torrent State: Completed | Allowing Seeding | "
                "[Progress: %s%%][Added On: %s]"
                "[Availability: %s%%][Time Left: %s]"
                "[Last active: %s] "
                "| [%s] | %s (%s)",
                round(torrent.progress * 100, 2),
                datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
                round(torrent.availability * 100, 2),
                timedelta(seconds=torrent.eta),
                datetime.fromtimestamp(torrent.last_activity),
                torrent.state_enum,
                torrent.name,
                torrent.hash,
            )
        else:
            self.logger.info(
                "Pausing Completed torrent: "
                "[Progress: %s%%][Added On: %s]"
                "[Availability: %s%%][Time Left: %s]"
                "[Last active: %s] "
                "| [%s] | %s (%s)",
                round(torrent.progress * 100, 2),
                datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
                round(torrent.availability * 100, 2),
                timedelta(seconds=torrent.eta),
                datetime.fromtimestamp(torrent.last_activity),
                torrent.state_enum,
                torrent.name,
                torrent.hash,
            )
            self.pause.add(torrent.hash)
            content_path = pathlib.Path(torrent.content_path)
            if content_path.is_dir() and content_path.name == torrent.name:
                torrent_folder = content_path
            else:
                if content_path.is_file() and content_path.parent.name == torrent.name:
                    torrent_folder = content_path.parent
                else:
                    torrent_folder = content_path
            self.files_to_cleanup.add((torrent.hash, torrent_folder))
            self.import_torrents.append(torrent)

    def _process_single_torrent_missing_files(self, torrent: qbittorrentapi.TorrentDictionary):
        # Sometimes Sonarr/Radarr does not automatically remove the
        # torrent for some reason,
        # this ensures that we can safely remove it if the client is reporting
        # the status of the client as "Missing files"
        self.logger.info(
            "Deleting torrent with missing files: "
            "[Progress: %s%%][Added On: %s]"
            "[Availability: %s%%][Time Left: %s]"
            "[Last active: %s] "
            "| [%s] | %s (%s)",
            round(torrent.progress * 100, 2),
            datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
            round(torrent.availability * 100, 2),
            timedelta(seconds=torrent.eta),
            datetime.fromtimestamp(torrent.last_activity),
            torrent.state_enum,
            torrent.name,
            torrent.hash,
        )
        # We do not want to blacklist these!!
        self.remove_from_qbit.add(torrent.hash)

    def _process_single_torrent_uploading(
        self, torrent: qbittorrentapi.TorrentDictionary, leave_alone: bool
    ):
        if leave_alone or torrent.state_enum == TorrentStates.FORCED_UPLOAD:
            self.logger.trace(
                "Torrent State: Queued Upload | Allowing Seeding | "
                "[Progress: %s%%][Added On: %s]"
                "[Availability: %s%%][Time Left: %s]"
                "[Last active: %s] "
                "| [%s] | %s (%s)",
                round(torrent.progress * 100, 2),
                datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
                round(torrent.availability * 100, 2),
                timedelta(seconds=torrent.eta),
                datetime.fromtimestamp(torrent.last_activity),
                torrent.state_enum,
                torrent.name,
                torrent.hash,
            )
        else:
            self.logger.info(
                "Pausing uploading torrent: "
                "[Progress: %s%%][Added On: %s]"
                "[Availability: %s%%][Time Left: %s]"
                "[Last active: %s] "
                "| [%s] | %s (%s)",
                round(torrent.progress * 100, 2),
                datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
                round(torrent.availability * 100, 2),
                timedelta(seconds=torrent.eta),
                datetime.fromtimestamp(torrent.last_activity),
                torrent.state_enum,
                torrent.name,
                torrent.hash,
            )
            self.pause.add(torrent.hash)

    def _process_single_torrent_already_cleaned_up(
        self, torrent: qbittorrentapi.TorrentDictionary
    ):
        self.logger.trace(
            "Skipping file check: Already been cleaned up | "
            "[Progress: %s%%][Added On: %s]"
            "[Availability: %s%%][Time Left: %s]"
            "[Last active: %s] "
            "| [%s] | %s (%s)",
            round(torrent.progress * 100, 2),
            datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
            round(torrent.availability * 100, 2),
            timedelta(seconds=torrent.eta),
            datetime.fromtimestamp(torrent.last_activity),
            torrent.state_enum,
            torrent.name,
            torrent.hash,
        )

    def _process_single_torrent_delete_slow(self, torrent: qbittorrentapi.TorrentDictionary):
        self.logger.trace(
            "Deleting slow torrent: "
            "[Progress: %s%%][Added On: %s]"
            "[Availability: %s%%][Time Left: %s]"
            "[Last active: %s] "
            "| [%s] | %s (%s)",
            round(torrent.progress * 100, 2),
            datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
            round(torrent.availability * 100, 2),
            timedelta(seconds=torrent.eta),
            datetime.fromtimestamp(torrent.last_activity),
            torrent.state_enum,
            torrent.name,
            torrent.hash,
        )
        self.delete.add(torrent.hash)

    def _process_single_torrent_delete_ratio_seed(self, torrent: qbittorrentapi.TorrentDictionary):
        self.logger.info(
            "Removing completed torrent: "
            "[Progress: %s%%][Added On: %s]"
            "[Ratio: %s%%][Seeding time: %s]"
            "[Last active: %s] "
            "| [%s] | %s (%s)",
            round(torrent.progress * 100, 2),
            datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
            torrent.ratio,
            timedelta(seconds=torrent.seeding_time),
            datetime.fromtimestamp(torrent.last_activity),
            torrent.state_enum,
            torrent.name,
            torrent.hash,
        )
        self.delete.add(torrent.hash)

    def _process_single_torrent_process_files(
        self, torrent: qbittorrentapi.TorrentDictionary, special_case: bool = False
    ):
        _remove_files = set()
        total = len(torrent.files)
        if total == 0:
            return
        elif special_case:
            self.special_casing_file_check.add(torrent.hash)
        for file in torrent.files:
            file_path = pathlib.Path(file.name)
            # Acknowledge files that already been marked as "Don't download"
            if file.priority == 0:
                total -= 1
                continue
            # A folder within the folder tree matched the terms
            # in FolderExclusionRegex, mark it for exclusion.
            if self.folder_exclusion_regex and any(
                self.folder_exclusion_regex_re.search(p.name.lower())
                for p in file_path.parents
                if (folder_match := p.name)
            ):
                self.logger.debug(
                    "Removing File: Not allowed | Parent: %s  | %s (%s) | %s ",
                    folder_match,
                    torrent.name,
                    torrent.hash,
                    file.name,
                )
                _remove_files.add(file.id)
                total -= 1
            # A file matched and entry in FileNameExclusionRegex, mark it for
            # exclusion.
            elif self.file_name_exclusion_regex and (
                (match := self.file_name_exclusion_regex_re.search(file_path.name))
                and match.group()
            ):
                self.logger.debug(
                    "Removing File: Not allowed | Name: %s  | %s (%s) | %s ",
                    match.group(),
                    torrent.name,
                    torrent.hash,
                    file.name,
                )
                _remove_files.add(file.id)
                total -= 1
            elif self.file_extension_allowlist and not (
                (match := self.file_extension_allowlist.search(file_path.suffix)) and match.group()
            ):
                self.logger.debug(
                    "Removing File: Not allowed | Extension: %s  | %s (%s) | %s ",
                    file_path.suffix,
                    torrent.name,
                    torrent.hash,
                    file.name,
                )
                _remove_files.add(file.id)
                total -= 1
            # If all files in the torrent are marked for exclusion then delete the
            # torrent.
            if total == 0:
                self.logger.info(
                    "Deleting All files ignored: "
                    "[Progress: %s%%][Added On: %s]"
                    "[Availability: %s%%][Time Left: %s]"
                    "[Last active: %s] "
                    "| [%s] | %s (%s)",
                    round(torrent.progress * 100, 2),
                    datetime.fromtimestamp(
                        self.recently_queue.get(torrent.hash, torrent.added_on)
                    ),
                    round(torrent.availability * 100, 2),
                    timedelta(seconds=torrent.eta),
                    datetime.fromtimestamp(torrent.last_activity),
                    torrent.state_enum,
                    torrent.name,
                    torrent.hash,
                )
                self.delete.add(torrent.hash)
            # Mark all bad files and folder for exclusion.
            elif _remove_files and torrent.hash not in self.change_priority:
                self.change_priority[torrent.hash] = list(_remove_files)
            elif _remove_files and torrent.hash in self.change_priority:
                self.change_priority[torrent.hash] = list(_remove_files)

        self.cleaned_torrents.add(torrent.hash)

    def _process_single_torrent_unprocessed(self, torrent: qbittorrentapi.TorrentDictionary):
        self.logger.trace(
            "Skipping torrent: Unresolved state: "
            "[Progress: %s%%][Added On: %s]"
            "[Availability: %s%%][Time Left: %s]"
            "[Last active: %s] "
            "| [%s] | %s (%s)",
            round(torrent.progress * 100, 2),
            datetime.fromtimestamp(self.recently_queue.get(torrent.hash, torrent.added_on)),
            round(torrent.availability * 100, 2),
            timedelta(seconds=torrent.eta),
            datetime.fromtimestamp(torrent.last_activity),
            torrent.state_enum,
            torrent.name,
            torrent.hash,
        )

    def _get_torrent_important_trackers(
        self, torrent: qbittorrentapi.TorrentDictionary
    ) -> tuple[set[str], set[str]]:
        current_trackers = {i.url for i in torrent.trackers}
        monitored_trackers = self._monitored_tracker_urls.intersection(current_trackers)
        need_to_be_added = self._add_trackers_if_missing.difference(current_trackers)
        monitored_trackers = monitored_trackers.union(need_to_be_added)
        return need_to_be_added, monitored_trackers

    @staticmethod
    def __return_max(x: dict):
        return x.get("Priority", -100)

    def _get_most_important_tracker_and_tags(
        self, monitored_trackers, removed
    ) -> tuple[dict, set[str]]:
        new_list = [
            i
            for i in self.monitored_trackers
            if (i.get("URI") in monitored_trackers) and i.get("RemoveIfExists") is not True
        ]
        _list_of_tags = [i.get("AddTags", []) for i in new_list if i.get("URI") not in removed]
        max_item = max(new_list, key=self.__return_max) if new_list else {}
        return max_item, set(itertools.chain.from_iterable(_list_of_tags))

    def _get_torrent_limit_meta(self, torrent: qbittorrentapi.TorrentDictionary):
        _, monitored_trackers = self._get_torrent_important_trackers(torrent)
        most_important_tracker, unique_tags = self._get_most_important_tracker_and_tags(
            monitored_trackers, {}
        )

        data_settings = {
            "ratio_limit": r
            if (
                r := most_important_tracker.get(
                    "MaxUploadRatio", self.seeding_mode_global_max_upload_ratio
                )
            )
            > 0
            else -5,
            "seeding_time_limit": r
            if (
                r := most_important_tracker.get(
                    "MaxSeedingTime", self.seeding_mode_global_max_seeding_time
                )
            )
            > 0
            else -5,
            "dl_limit": r
            if (
                r := most_important_tracker.get(
                    "DownloadRateLimit", self.seeding_mode_global_download_limit
                )
            )
            > 0
            else -5,
            "up_limit": r
            if (
                r := most_important_tracker.get(
                    "UploadRateLimit", self.seeding_mode_global_upload_limit
                )
            )
            > 0
            else -5,
            "super_seeding": most_important_tracker.get("SuperSeedMode", torrent.super_seeding),
            "max_eta": most_important_tracker.get("MaximumETA", self.maximum_eta),
        }

        data_torrent = {
            "ratio_limit": r if (r := torrent.ratio_limit) > 0 else -5,
            "seeding_time_limit": r if (r := torrent.seeding_time_limit) > 0 else -5,
            "dl_limit": r if (r := torrent.dl_limit) > 0 else -5,
            "up_limit": r if (r := torrent.up_limit) > 0 else -5,
            "super_seeding": torrent.super_seeding,
        }
        return data_settings, data_torrent

    def _should_leave_alone(
        self, torrent: qbittorrentapi.TorrentDictionary
    ) -> tuple[bool, int, bool]:
        return_value = True
        remove_torrent = False
        if torrent.super_seeding or torrent.state_enum == TorrentStates.FORCED_UPLOAD:
            return return_value, -1  # Do not touch super seeding torrents.
        data_settings, data_torrent = self._get_torrent_limit_meta(torrent)
        self.logger.trace("Config Settings for torrent [%s]: %r", torrent.name, data_settings)
        self.logger.trace("Torrent Settings for torrent [%s]: %r", torrent.name, data_torrent)
        # self.logger.trace("%r", torrent)

        ratio_limit_dat = data_settings.get("ratio_limit", -5)
        ratio_limit_tor = data_torrent.get("ratio_limit", -5)
        seeding_time_limit_dat = data_settings.get("seeding_time_limit", -5)
        seeding_time_limit_tor = data_torrent.get("seeding_time_limit", -5)

        seeding_time_limit = max(seeding_time_limit_dat, seeding_time_limit_tor)
        ratio_limit = max(ratio_limit_dat, ratio_limit_tor)

        if self.seeding_mode_global_remove_torrent != -1 and self.remove_torrent(
            torrent, seeding_time_limit, ratio_limit
        ):
            remove_torrent = True
            return_value = False
        else:
            if torrent.ratio >= ratio_limit:
                return_value = False  # Seeding ratio met - Can be cleaned up.
            if torrent.seeding_time >= seeding_time_limit:
                return_value = False  # Seeding time met - Can be cleaned up.
        if data_settings.get("super_seeding", False) or data_torrent.get("super_seeding", False):
            return_value = True
        if return_value and "qBitrr-allowed_seeding" not in torrent.tags:
            torrent.add_tags(tags=["qBitrr-allowed_seeding"])
        elif not return_value and "qBitrr-allowed_seeding" in torrent.tags:
            torrent.remove_tags(tags=["qBitrr-allowed_seeding"])
        return (
            return_value,
            data_settings.get("max_eta", self.maximum_eta),
            remove_torrent,
        )  # Seeding is not complete needs more time

    def _process_single_torrent_trackers(self, torrent: qbittorrentapi.TorrentDictionary):
        if torrent.hash in self.tracker_delay:
            return
        self.tracker_delay.add(torrent.hash)
        _remove_urls = set()
        need_to_be_added, monitored_trackers = self._get_torrent_important_trackers(torrent)
        if need_to_be_added:
            torrent.add_trackers(need_to_be_added)
        with contextlib.suppress(BaseException):
            for tracker in torrent.trackers:
                if (
                    self.remove_dead_trackers
                    and (
                        any(tracker.msg == m for m in self.seeding_mode_global_bad_tracker_msg)
                    )  # TODO: Add more messages
                ) or tracker.url in self._remove_trackers_if_exists:
                    _remove_urls.add(tracker.url)
        if _remove_urls:
            self.logger.trace(
                "Removing trackers from torrent: %s (%s) - %s",
                torrent.name,
                torrent.hash,
                _remove_urls,
            )
            with contextlib.suppress(qbittorrentapi.Conflict409Error):
                torrent.remove_trackers(_remove_urls)
        most_important_tracker, unique_tags = self._get_most_important_tracker_and_tags(
            monitored_trackers, _remove_urls
        )
        if monitored_trackers and most_important_tracker:
            # Only use globals if there is not a configured equivalent value on the
            # highest priority tracker
            data = {
                "ratio_limit": r
                if (
                    r := most_important_tracker.get(
                        "MaxUploadRatio", self.seeding_mode_global_max_upload_ratio
                    )
                )
                > 0
                else None,
                "seeding_time_limit": r
                if (
                    r := most_important_tracker.get(
                        "MaxSeedingTime", self.seeding_mode_global_max_seeding_time
                    )
                )
                > 0
                else None,
            }
            if any(r is not None for r in data):
                if (
                    (_l1 := data.get("seeding_time_limit"))
                    and _l1 > 0
                    and torrent.seeding_time_limit != data.get("seeding_time_limit")
                ):
                    data.pop("seeding_time_limit")
                if (
                    (_l2 := data.get("ratio_limit"))
                    and _l2 > 0
                    and torrent.seeding_time_limit != data.get("ratio_limit")
                ):
                    data.pop("ratio_limit")

                if not _l1:
                    data["seeding_time_limit"] = None
                elif _l1 < 0:
                    data["seeding_time_limit"] = None
                if not _l2:
                    data["ratio_limit"] = None
                elif _l2 < 0:
                    data["ratio_limit"] = None

                if any(v is not None for v in data.values()) and data:
                    with contextlib.suppress(Exception):
                        torrent.set_share_limits(**data)
            if (
                r := most_important_tracker.get(
                    "DownloadRateLimit", self.seeding_mode_global_download_limit
                )
                != 0
                and torrent.dl_limit != r
            ):
                torrent.set_download_limit(limit=r)
            elif r < 0:
                torrent.set_upload_limit(limit=-1)
            if (
                r := most_important_tracker.get(
                    "UploadRateLimit", self.seeding_mode_global_upload_limit
                )
                != 0
                and torrent.up_limit != r
            ):
                torrent.set_upload_limit(limit=r)
            elif r < 0:
                torrent.set_upload_limit(limit=-1)
            if (
                r := most_important_tracker.get("SuperSeedMode", False)
                and torrent.super_seeding != r
            ):
                torrent.set_super_seeding(enabled=r)

        else:
            data = {
                "ratio_limit": r if (r := self.seeding_mode_global_max_upload_ratio) > 0 else None,
                "seeding_time_limit": r
                if (r := self.seeding_mode_global_max_seeding_time) > 0
                else None,
            }
            if any(r is not None for r in data):
                if (
                    (_l1 := data.get("seeding_time_limit"))
                    and _l1 > 0
                    and torrent.seeding_time_limit != data.get("seeding_time_limit")
                ):
                    data.pop("seeding_time_limit")
                if (
                    (_l2 := data.get("ratio_limit"))
                    and _l2 > 0
                    and torrent.seeding_time_limit != data.get("ratio_limit")
                ):
                    data.pop("ratio_limit")
                if not _l1:
                    data["seeding_time_limit"] = None
                elif _l1 < 0:
                    data["seeding_time_limit"] = None
                if not _l2:
                    data["ratio_limit"] = None
                elif _l2 < 0:
                    data["ratio_limit"] = None
                if any(v is not None for v in data.values()) and data:
                    with contextlib.suppress(Exception):
                        torrent.set_share_limits(**data)

            if r := self.seeding_mode_global_download_limit != 0 and torrent.dl_limit != r:
                torrent.set_download_limit(limit=r)
            elif r < 0:
                torrent.set_download_limit(limit=-1)
            if r := self.seeding_mode_global_upload_limit != 0 and torrent.up_limit != r:
                torrent.set_upload_limit(limit=r)
            elif r < 0:
                torrent.set_upload_limit(limit=-1)

        if unique_tags:
            current_tags = set(torrent.tags.split(", "))
            add_tags = unique_tags.difference(current_tags)
            if add_tags:
                torrent.add_tags(add_tags)

    def _process_single_torrent(self, torrent: qbittorrentapi.TorrentDictionary):
        if torrent.category != RECHECK_CATEGORY:
            self.manager.qbit_manager.cache[torrent.hash] = torrent.category
        self._process_single_torrent_trackers(torrent)
        self.manager.qbit_manager.name_cache[torrent.hash] = torrent.name
        time_now = time.time()
        try:
            leave_alone, _tracker_max_eta, remove_torrent = self._should_leave_alone(torrent)
        except BaseException as e:
            self.logger.warning(e)
            raise DelayLoopException(length=300, type="qbit")
        self.logger.trace(
            "Torrent [%s]: Leave Alone (allow seeding): %s, Max ETA: %s",
            torrent.name,
            leave_alone,
            _tracker_max_eta,
        )
        maximum_eta = _tracker_max_eta
        if remove_torrent and not leave_alone and torrent.amount_left == 0:
            self._process_single_torrent_delete_ratio_seed(torrent)
        elif torrent.category == FAILED_CATEGORY:
            # Bypass everything if manually marked as failed
            self._process_single_torrent_failed_cat(torrent)
        elif torrent.category == RECHECK_CATEGORY:
            # Bypass everything else if manually marked for rechecking
            self._process_single_torrent_recheck_cat(torrent)
        elif self.is_ignored_state(torrent):
            self._process_single_torrent_ignored(torrent)

        elif (
            torrent.state_enum.is_downloading
            and torrent.state_enum != TorrentStates.METADATA_DOWNLOAD
            and torrent.hash not in self.special_casing_file_check
            and torrent.hash not in self.cleaned_torrents
        ):
            self._process_single_torrent_process_files(torrent, True)
        elif torrent.hash in self.timed_ignore_cache:
            # Do not touch torrents recently resumed/reached (A torrent can temporarily
            # stall after being resumed from a paused state).
            self._process_single_torrent_added_to_ignore_cache(torrent)

        elif torrent.state_enum == TorrentStates.QUEUED_UPLOAD:
            self._process_single_torrent_queued_upload(torrent, leave_alone)
        elif torrent.state_enum in (
            TorrentStates.METADATA_DOWNLOAD,
            TorrentStates.STALLED_DOWNLOAD,
        ):
            self._process_single_torrent_stalled_torrent(torrent, "Stalled State")
        elif (
            torrent.progress >= self.maximum_deletable_percentage
            and self.is_complete_state(torrent) is False
        ) and torrent.hash in self.cleaned_torrents:
            self._process_single_torrent_percentage_threshold(torrent, maximum_eta)
        # Resume monitored downloads which have been paused.
        elif torrent.state_enum == TorrentStates.PAUSED_DOWNLOAD and torrent.amount_left != 0:
            self._process_single_torrent_paused(torrent)
        # Ignore torrents which have been submitted to their respective Arr
        # instance for import.
        elif (
            torrent.hash in self.manager.managed_objects[torrent.category].sent_to_scan_hashes
        ) and torrent.hash in self.cleaned_torrents:
            self._process_single_torrent_already_sent_to_scan(torrent)

        # Sometimes torrents will error, this causes them to be rechecked so they
        # complete downloading.
        elif torrent.state_enum == TorrentStates.ERROR:
            self._process_single_torrent_errored(torrent)
        # If a torrent was not just added,
        # and the amount left to download is 0 and the torrent
        # is Paused tell the Arr tools to process it.
        elif (
            torrent.added_on > 0
            and torrent.completion_on
            and torrent.amount_left == 0
            and torrent.state_enum != TorrentStates.PAUSED_UPLOAD
            and self.is_complete_state(torrent)
            and torrent.content_path
            and torrent.completion_on < time_now - 60
        ):
            self._process_single_torrent_fully_completed_torrent(torrent, leave_alone)
        elif torrent.state_enum == TorrentStates.MISSING_FILES:
            self._process_single_torrent_missing_files(torrent)
        # If a torrent is Uploading Pause it, as long as its not being Forced Uploaded.
        elif (
            self.is_uploading_state(torrent)
            and torrent.seeding_time > 1
            and torrent.amount_left == 0
            and torrent.added_on > 0
            and torrent.content_path
            and self.seeding_mode_global_remove_torrent != -1
        ) and torrent.hash in self.cleaned_torrents:
            self._process_single_torrent_uploading(torrent, leave_alone)
        # Mark a torrent for deletion
        elif (
            torrent.state_enum != TorrentStates.PAUSED_DOWNLOAD
            and torrent.state_enum.is_downloading
            and self.recently_queue.get(torrent.hash, torrent.added_on)
            < time_now - self.ignore_torrents_younger_than
            and 0 < maximum_eta < torrent.eta
            and not self.do_not_remove_slow
        ):
            self._process_single_torrent_delete_slow(torrent)
        # Process uncompleted torrents
        elif torrent.state_enum.is_downloading:
            # If a torrent availability hasn't reached 100% or more within the configurable
            # "IgnoreTorrentsYoungerThan" variable, mark it for deletion.
            if (
                self.recently_queue.get(torrent.hash, torrent.added_on)
                < time_now - self.ignore_torrents_younger_than
                and torrent.availability < 1
            ) and torrent.hash in self.cleaned_torrents:
                self._process_single_torrent_stalled_torrent(torrent, "Unavailable")
            else:
                if torrent.hash in self.cleaned_torrents:
                    self._process_single_torrent_already_cleaned_up(torrent)
                    return
                # A downloading torrent is not stalled, parse its contents.
                self._process_single_torrent_process_files(torrent)
        else:
            self._process_single_torrent_unprocessed(torrent)

    def remove_torrent(
        self, torrent: qbittorrentapi.TorrentDictionary, seeding_time_limit, ratio_limit
    ):
        if (
            self.seeding_mode_global_remove_torrent == 4
            and torrent.ratio >= ratio_limit
            and torrent.seeding_time >= seeding_time_limit
        ):
            return True
        if self.seeding_mode_global_remove_torrent == 3 and (
            torrent.ratio >= ratio_limit or torrent.seeding_time >= seeding_time_limit
        ):
            return True
        elif (
            self.seeding_mode_global_remove_torrent == 2
            and torrent.seeding_time >= seeding_time_limit
        ):
            return True
        elif self.seeding_mode_global_remove_torrent == 1 and torrent.ratio >= ratio_limit:
            return True
        else:
            return False

    def refresh_download_queue(self):
        self.queue = self.get_queue()
        self.cache = {
            entry["downloadId"]: entry["id"] for entry in self.queue if entry.get("downloadId")
        }
        if self.type == "sonarr":
            self.requeue_cache = defaultdict(set)
            for entry in self.queue:
                if r := entry.get("episodeId"):
                    self.requeue_cache[entry["id"]].add(r)
            self.queue_file_ids = {
                entry["episodeId"] for entry in self.queue if entry.get("episodeId")
            }
            if self.model_queue:
                self.model_queue.delete().where(
                    self.model_queue.EntryId.not_in(list(self.queue_file_ids))
                )
        elif self.type == "radarr":
            self.requeue_cache = {
                entry["id"]: entry["movieId"] for entry in self.queue if entry.get("movieId")
            }
            self.queue_file_ids = {
                entry["movieId"] for entry in self.queue if entry.get("movieId")
            }
            if self.model_queue:
                self.model_queue.delete().where(
                    self.model_queue.EntryId.not_in(list(self.queue_file_ids))
                )

        self._update_bad_queue_items()

    def get_queue(
        self,
        page=1,
        page_size=10000,
        sort_direction="ascending",
        sort_key="timeLeft",
        messages: bool = True,
    ):
        completed = True
        while completed:
            completed = False
            try:
                res = self.client.get_queue(
                    page=page, page_size=page_size, sort_key=sort_key, sort_dir=sort_direction
                )
            except (
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ContentDecodingError,
                requests.exceptions.ConnectionError,
            ) as e:
                completed = True
        res = res.get("records", [])
        return res

    def _update_bad_queue_items(self):
        if not self.arr_error_codes_to_blocklist:
            return
        _temp = self.get_queue()
        _temp = filter(
            lambda x: x.get("status") == "completed"
            and x.get("trackedDownloadState") == "importPending"
            and x.get("trackedDownloadStatus") == "warning",
            _temp,
        )
        _path_filter = set()
        _temp = list(_temp)
        for entry in _temp:
            messages = entry.get("statusMessages", [])
            output_path = entry.get("outputPath")
            for m in messages:
                title = m.get("title")
                if not title:
                    continue
                for _m in m.get("messages", []):
                    if _m in self.arr_error_codes_to_blocklist:
                        e = entry.get("downloadId")
                        _path_filter.add((e, pathlib.Path(output_path).joinpath(title)))
                        # self.downloads_with_bad_error_message_blocklist.add(e)
        if len(_path_filter):
            self.needs_cleanup = True
        self.files_to_explicitly_delete = iter(_path_filter.copy())

    def force_grab(self):
        return  # TODO: This may not be needed, pending more testing before it is enabled
        _temp = self.get_queue()
        _temp = filter(
            lambda x: x.get("status") == "delay",
            _temp,
        )
        ids = set()
        for entry in _temp:
            if id_ := entry.get("id"):
                ids.add(id_)
                self.logger.notice("Attempting to force grab: %s =  %s", id_, entry.get("title"))
        if ids:
            with ThreadPoolExecutor(max_workers=16) as executor:
                executor.map(self._force_grab, ids)

    def _force_grab(self, id_):
        try:
            path = f"queue/grab/{id_}"
            res = self.client._post(path, self.client.ver_uri)
            self.logger.trace("Successful Grab: %s", id_)
            return res
        except Exception:
            self.logger.error("Exception when trying to force grab - %s", id_)

    def register_search_mode(self):
        if self.search_setup_completed:
            return
        if self.search_missing is False:
            self.search_setup_completed = True
            return
        if not self.arr_db_file.exists():
            self.search_missing = False
            return
        else:
            self.arr_db = SqliteDatabase(None)
            self.arr_db.init(f"file:{self.arr_db_file}?mode=ro", uri=True)
            self.arr_db.connect()

        self.db = SqliteDatabase(None)
        self.db.init(
            str(self.search_db_file),
            pragmas={
                "journal_mode": "wal",
                "cache_size": -1 * 64000,  # 64MB
                "foreign_keys": 1,
                "ignore_check_constraints": 0,
                "synchronous": 0,
            },
        )

        db1, db2, db3 = self._get_models()

        class Files(db1):
            class Meta:
                database = self.db

        class Queue(db2):
            class Meta:
                database = self.db

        class PersistingQueue(FilesQueued):
            class Meta:
                database = self.db

        self.db.connect()
        if db3:

            class Series(db3):
                class Meta:
                    database = self.db

            self.db.create_tables([Files, Queue, PersistingQueue, Series])
            self.series_file_model = Series
        else:
            self.db.create_tables([Files, Queue, PersistingQueue])

        self.model_file = Files
        self.model_queue = Queue
        self.persistent_queue = PersistingQueue

        db1, db2, db3 = self._get_arr_modes()

        class Files(db1):
            class Meta:
                database = self.arr_db
                if self.type == "sonarr":
                    table_name = "Episodes"
                elif self.type == "radarr":
                    table_name = "Movies"

        class Commands(db2):
            class Meta:
                database = self.arr_db
                table_name = "Commands"

        if self.type == "sonarr":

            class Series(db3):
                class Meta:
                    database = self.arr_db
                    table_name = "Series"

            self.model_arr_series_file = Series

        elif self.type == "radarr":

            class Movies(db3):
                class Meta:
                    database = self.arr_db
                    table_name = "MovieMetadata"

            self.model_arr_movies_file = Movies

        self.model_arr_file = Files
        self.model_arr_command = Commands
        self.search_setup_completed = True

    def run_request_search(self):
        if self.request_search_timer is None or (
            self.request_search_timer > time.time() - self.search_requests_every_x_seconds
        ):
            return None
        self.register_search_mode()
        if not self.search_missing:
            return None
        self.logger.notice("Starting Request search")

        while True:
            try:
                self.db_request_update()
                try:
                    for entry in self.db_get_request_files():
                        while self.maybe_do_search(entry, request=True) is False:
                            time.sleep(30)
                    self.request_search_timer = time.time()
                    return
                except NoConnectionrException as e:
                    self.logger.error(e.message)
                    raise DelayLoopException(length=300, type=e.type)
                except DelayLoopException:
                    raise
                except Exception as e:
                    self.logger.exception(e, exc_info=sys.exc_info())
                time.sleep(LOOP_SLEEP_TIMER)
            except DelayLoopException as e:
                if e.type == "qbit":
                    self.logger.critical(
                        "Failed to connected to qBit client, sleeping for %s",
                        timedelta(seconds=e.length),
                    )
                elif e.type == "internet":
                    self.logger.critical(
                        "Failed to connected to the internet, sleeping for %s",
                        timedelta(seconds=e.length),
                    )
                elif e.type == "arr":
                    self.logger.critical(
                        "Failed to connected to the Arr instance, sleeping for %s",
                        timedelta(seconds=e.length),
                    )
                elif e.type == "delay":
                    self.logger.critical(
                        "Forced delay due to temporary issue with environment, sleeping for %s",
                        timedelta(seconds=e.length),
                    )
                elif e.type == "no_downloads":
                    self.logger.debug(
                        "No downloads in category, sleeping for %s",
                        timedelta(seconds=e.length),
                    )
                time.sleep(e.length)

    def get_year_search(self) -> tuple[list[int], int]:
        with self.db.atomic():
            if self.type == "radarr":
                if self.search_in_reverse:
                    years_query = (
                        self.model_arr_movies_file.select(
                            self.model_arr_movies_file.Year.distinct()
                        )
                        .where(
                            self.model_arr_movies_file.Year
                            <= datetime.now().year & self.model_arr_movies_file.Year
                            != 0
                        )
                        .order_by(self.model_arr_movies_file.Year.asc())
                        .execute()
                    )
                else:
                    years_query = (
                        self.model_arr_movies_file.select(
                            self.model_arr_movies_file.Year.distinct()
                        )
                        .where(
                            self.model_arr_movies_file.Year
                            <= datetime.now().year & self.model_arr_movies_file.Year
                            != 0
                        )
                        .order_by(self.model_arr_movies_file.Year.desc())
                        .execute()
                    )
                years = [y.Year for y in years_query]
                self.logger.trace("Years: %s", years)
                years_count = len(years)
            elif self.type == "sonarr":
                self.model_arr_file: EpisodesModel
                if self.search_in_reverse:
                    years_query = (
                        self.model_arr_file.select(
                            fn.Substr(self.model_arr_file.AirDate, 1, 4).distinct().alias("Year")
                        )
                        .where(fn.Substr(self.model_arr_file.AirDate, 1, 4) <= datetime.now())
                        .order_by(fn.Substr(self.model_arr_file.AirDate, 1, 4).asc())
                        .execute()
                    )
                else:
                    years_query = (
                        self.model_arr_file.select(
                            fn.Substr(self.model_arr_file.AirDate, 1, 4).distinct().alias("Year")
                        )
                        .where(fn.Substr(self.model_arr_file.AirDate, 1, 4) <= datetime.now())
                        .order_by(fn.Substr(self.model_arr_file.AirDate, 1, 4).desc())
                        .execute()
                    )
                years = [y.Year for y in years_query]
                self.logger.trace("Years: %s", years)
                years_count = len(years)
        self.logger.trace("Years count: %s, Years: %s", years_count, years)
        return years, years_count

    def run_search_loop(self) -> NoReturn:
        run_logs(self.logger)
        try:
            self.register_search_mode()
            if not self.search_missing:
                return None
            loop_timer = timedelta(minutes=15)
            timer = datetime.now()
            years_index = 0
            while True:
                if self.loop_completed:
                    years_index = 0
                    timer = datetime.now()
                if self.search_by_year:
                    if years_index == 0:
                        years, years_count = self.get_year_search()
                        try:
                            self.search_current_year = years[years_index]
                        except BaseException:
                            self.search_current_year = years[: years_index + 1]
                    self.logger.debug(
                        "Current year %s",
                        self.search_current_year,
                    )
                try:
                    self.db_maybe_reset_entry_searched_state()
                    self.refresh_download_queue()
                    self.db_update()
                    self.run_request_search()
                    self.force_grab()
                    try:
                        if self.search_by_year:
                            if years.index(self.search_current_year) != years_count - 1:
                                years_index += 1
                                self.search_current_year = years[years_index]
                            elif datetime.now() >= (timer + loop_timer):
                                self.refresh_download_queue()
                                self.force_grab()
                                raise RestartLoopException
                        elif datetime.now() >= (timer + loop_timer):
                            self.refresh_download_queue()
                            self.force_grab()
                            raise RestartLoopException
                        for entry, todays, limit_bypass, series_search in self.db_get_files():
                            self.logger.trace("Running search for %s", entry.Title)
                            while (
                                self.maybe_do_search(
                                    entry,
                                    todays=todays,
                                    bypass_limit=limit_bypass,
                                    series_search=series_search,
                                )
                            ) is False:
                                self.logger.debug("Waiting for active search commands")
                                time.sleep(30)
                    except RestartLoopException:
                        self.loop_completed = True
                        self.logger.info("Loop timer elapsed, restarting it.")
                    except NoConnectionrException as e:
                        self.logger.error(e.message)
                        self.manager.qbit_manager.should_delay_torrent_scan = True
                        raise DelayLoopException(length=300, type=e.type)
                    except DelayLoopException:
                        raise
                    except ValueError:
                        self.logger.info("Loop completed, restarting it.")
                        self.loop_completed = True
                    except qbittorrentapi.exceptions.APIConnectionError as e:
                        self.logger.warning(e)
                        raise DelayLoopException(length=300, type="qbit")
                    except Exception as e:
                        self.logger.exception(e, exc_info=sys.exc_info())
                    time.sleep(LOOP_SLEEP_TIMER)
                except DelayLoopException as e:
                    if e.type == "qbit":
                        self.logger.critical(
                            "Failed to connected to qBit client, sleeping for %s",
                            timedelta(seconds=e.length),
                        )
                    elif e.type == "internet":
                        self.logger.critical(
                            "Failed to connected to the internet, sleeping for %s",
                            timedelta(seconds=e.length),
                        )
                    elif e.type == "arr":
                        self.logger.critical(
                            "Failed to connected to the Arr instance, sleeping for %s",
                            timedelta(seconds=e.length),
                        )
                    elif e.type == "delay":
                        self.logger.critical(
                            "Forced delay due to temporary issue with environment, "
                            "sleeping for %s",
                            timedelta(seconds=e.length),
                        )
                    time.sleep(e.length)
                    self.manager.qbit_manager.should_delay_torrent_scan = False
                except KeyboardInterrupt:
                    self.logger.hnotice("Detected Ctrl+C - Terminating process")
                    sys.exit(0)
                else:
                    time.sleep(5)
        except KeyboardInterrupt:
            self.logger.hnotice("Detected Ctrl+C - Terminating process")
            sys.exit(0)

    def run_torrent_loop(self) -> NoReturn:
        run_logs(self.logger)
        self.logger.hnotice("Starting torrent monitoring for %s", self._name)
        while True:
            try:
                try:
                    try:
                        if not self.manager.qbit_manager.is_alive:
                            raise NoConnectionrException(
                                "Could not connect to qBit client.", type="qbit"
                            )
                        if not self.is_alive:
                            raise NoConnectionrException(
                                f"Could not connect to {self.uri}", type="arr"
                            )
                        self.process_torrents()
                    except NoConnectionrException as e:
                        self.logger.error(e.message)
                        self.manager.qbit_manager.should_delay_torrent_scan = True
                        raise DelayLoopException(length=300, type="arr")
                    except qbittorrentapi.exceptions.APIConnectionError as e:
                        self.logger.warning(e)
                        raise DelayLoopException(length=300, type="qbit")
                    except qbittorrentapi.exceptions.APIError as e:
                        self.logger.warning(e)
                        raise DelayLoopException(length=300, type="qbit")
                    except DelayLoopException:
                        raise
                    except KeyboardInterrupt:
                        self.logger.hnotice("Detected Ctrl+C - Terminating process")
                        sys.exit(0)
                    except Exception as e:
                        self.logger.error(e, exc_info=sys.exc_info())
                    time.sleep(LOOP_SLEEP_TIMER)
                except DelayLoopException as e:
                    if e.type == "qbit":
                        self.logger.critical(
                            "Failed to connected to qBit client, sleeping for %s",
                            timedelta(seconds=e.length),
                        )
                    elif e.type == "internet":
                        self.logger.critical(
                            "Failed to connected to the internet, sleeping for %s",
                            timedelta(seconds=e.length),
                        )
                    elif e.type == "arr":
                        self.logger.critical(
                            "Failed to connected to the Arr instance, sleeping for %s",
                            timedelta(seconds=e.length),
                        )
                    elif e.type == "delay":
                        self.logger.critical(
                            "Forced delay due to temporary issue with environment, "
                            "sleeping for %s.",
                            timedelta(seconds=e.length),
                        )
                    elif e.type == "no_downloads":
                        self.logger.debug(
                            "No downloads in category, sleeping for %s",
                            timedelta(seconds=e.length),
                        )
                    time.sleep(e.length)
                    self.manager.qbit_manager.should_delay_torrent_scan = False
                except KeyboardInterrupt:
                    self.logger.hnotice("Detected Ctrl+C - Terminating process")
                    sys.exit(0)
            except KeyboardInterrupt:
                self.logger.hnotice("Detected Ctrl+C - Terminating process")
                sys.exit(0)

    def spawn_child_processes(self):
        _temp = []
        if self.search_missing:
            self.process_search_loop = pathos.helpers.mp.Process(
                target=self.run_search_loop, daemon=True
            )
            self.manager.qbit_manager.child_processes.append(self.process_search_loop)
            _temp.append(self.process_search_loop)
        if not any([QBIT_DISABLED, SEARCH_ONLY]):
            self.process_torrent_loop = pathos.helpers.mp.Process(
                target=self.run_torrent_loop, daemon=True
            )
            self.manager.qbit_manager.child_processes.append(self.process_torrent_loop)
            _temp.append(self.process_torrent_loop)

        return len(_temp), _temp


class PlaceHolderArr(Arr):
    def __init__(
        self,
        name: str,
        manager: ArrManager,
    ):
        if name in manager.groups:
            raise OSError("Group '{name}' has already been registered.")
        self._name = name.title()
        self.category = name
        self.manager = manager
        self.queue = []
        self.cache = {}
        self.requeue_cache = {}
        self.recently_queue = {}
        self.sent_to_scan = set()
        self.sent_to_scan_hashes = set()
        self.files_probed = set()
        self.import_torrents = []
        self.change_priority = {}
        self.recheck = set()
        self.pause = set()
        self.skip_blacklist = set()
        self.remove_from_qbit = set()
        self.delete = set()
        self.resume = set()
        self.expiring_bool = ExpiringSet(max_age_seconds=10)
        self.ignore_torrents_younger_than = CONFIG.get(
            "Settings.IgnoreTorrentsYoungerThan", fallback=600
        )
        self.timed_ignore_cache = ExpiringSet(max_age_seconds=self.ignore_torrents_younger_than)
        self.timed_skip = ExpiringSet(max_age_seconds=self.ignore_torrents_younger_than)
        self.tracker_delay = ExpiringSet(max_age_seconds=600)
        self._LOG_LEVEL = self.manager.qbit_manager.logger.level
        self.logger = logging.getLogger(f"qBitrr.{self._name}")
        run_logs(self.logger)
        self.search_missing = False
        self.session = None
        self.logger.hnotice("Starting %s monitor", self._name)

    def _process_errored(self):
        # Recheck all torrents marked for rechecking.
        if not self.recheck:
            return
        temp = defaultdict(list)
        updated_recheck = []
        for h in self.recheck:
            updated_recheck.append(h)
            if c := self.manager.qbit_manager.cache.get(h):
                temp[c].append(h)
        self.manager.qbit.torrents_recheck(torrent_hashes=updated_recheck)
        for k, v in temp.items():
            self.manager.qbit.torrents_set_category(torrent_hashes=v, category=k)

        for k in updated_recheck:
            self.timed_ignore_cache.add(k)
        self.recheck.clear()

    def _process_failed(self):
        if not (self.delete or self.skip_blacklist):
            return
        to_delete_all = self.delete
        skip_blacklist = {i.upper() for i in self.skip_blacklist}
        if to_delete_all:
            for arr in self.manager.managed_objects.values():
                if payload := arr.process_entries(to_delete_all):
                    for entry, hash_ in payload:
                        if hash_ in arr.cache:
                            arr._process_failed_individual(
                                hash_=hash_, entry=entry, skip_blacklist=skip_blacklist
                            )
        if self.remove_from_qbit or self.skip_blacklist or to_delete_all:
            # Remove all bad torrents from the Client.
            temp_to_delete = set()
            if to_delete_all:
                self.manager.qbit.torrents_delete(hashes=to_delete_all, delete_files=True)
            if self.remove_from_qbit or self.skip_blacklist:
                temp_to_delete = self.remove_from_qbit.union(self.skip_blacklist)
                self.manager.qbit.torrents_delete(hashes=temp_to_delete, delete_files=True)
            to_delete_all = to_delete_all.union(temp_to_delete)
            for h in to_delete_all:
                if h in self.manager.qbit_manager.name_cache:
                    del self.manager.qbit_manager.name_cache[h]
                if h in self.manager.qbit_manager.cache:
                    del self.manager.qbit_manager.cache[h]
        self.skip_blacklist.clear()
        self.remove_from_qbit.clear()
        self.delete.clear()

    def process(self):
        self._process_errored()
        self._process_failed()

    def process_torrents(self):
        try:
            try:
                completed = True
                while completed:
                    try:
                        completed = False
                        torrents = self.manager.qbit_manager.client.torrents.info(
                            status_filter="all",
                            category=self.category,
                            sort="added_on",
                            reverse=False,
                        )
                    except qbittorrentapi.exceptions.APIError:
                        completed = True
                torrents = [t for t in torrents if hasattr(t, "category")]
                if not len(torrents):
                    raise DelayLoopException(length=5, type="no_downloads")
                if has_internet() is False:
                    self.manager.qbit_manager.should_delay_torrent_scan = True
                    raise DelayLoopException(length=NO_INTERNET_SLEEP_TIMER, type="internet")
                if self.manager.qbit_manager.should_delay_torrent_scan:
                    raise DelayLoopException(length=NO_INTERNET_SLEEP_TIMER, type="delay")
                for torrent in torrents:
                    if torrent.category != RECHECK_CATEGORY:
                        self.manager.qbit_manager.cache[torrent.hash] = torrent.category
                    self.manager.qbit_manager.name_cache[torrent.hash] = torrent.name
                    if torrent.category == FAILED_CATEGORY:
                        # Bypass everything if manually marked as failed
                        self._process_single_torrent_failed_cat(torrent)
                    elif torrent.category == RECHECK_CATEGORY:
                        # Bypass everything else if manually marked for rechecking
                        self._process_single_torrent_recheck_cat(torrent)
                self.process()
            except NoConnectionrException as e:
                self.logger.error(e.message)
            except qbittorrentapi.exceptions.APIError as e:
                self.logger.error("The qBittorrent API returned an unexpected error")
                self.logger.debug("Unexpected APIError from qBitTorrent", exc_info=e)
                raise DelayLoopException(length=300, type="qbit")
            except qbittorrentapi.exceptions.APIConnectionError as e:
                self.logger.warning("Max retries exceeded")
                raise DelayLoopException(length=300, type="qbit")
            except DelayLoopException:
                raise
            except KeyboardInterrupt:
                self.logger.hnotice("Detected Ctrl+C - Terminating process")
                sys.exit(0)
            except Exception as e:
                self.logger.error(e, exc_info=sys.exc_info())
        except KeyboardInterrupt:
            self.logger.hnotice("Detected Ctrl+C - Terminating process")
            sys.exit(0)
        except DelayLoopException:
            raise

    def run_search_loop(self):
        return


class ArrManager:
    def __init__(self, qbitmanager: qBitManager):
        self.groups: set[str] = set()
        self.uris: set[str] = set()
        self.special_categories: set[str] = {FAILED_CATEGORY, RECHECK_CATEGORY}
        self.category_allowlist: set[str] = self.special_categories.copy()
        self.completed_folders: set[pathlib.Path] = set()
        self.managed_objects: dict[str, Arr] = {}
        self.qbit: qbittorrentapi.Client = qbitmanager.client
        self.qbit_manager: qBitManager = qbitmanager
        self.ffprobe_available: bool = self.qbit_manager.ffprobe_downloader.probe_path.exists()
        self.logger = logging.getLogger(
            "qBitrr.ArrManager",
        )
        run_logs(self.logger)
        if not self.ffprobe_available and not any([QBIT_DISABLED, SEARCH_ONLY]):
            self.logger.error(
                "'%s' was not found, disabling all functionality dependant on it",
                self.qbit_manager.ffprobe_downloader.probe_path,
            )

    def build_arr_instances(self):
        for key in CONFIG.sections():
            if search := re.match("(rad|son|anim)arr.*", key, re.IGNORECASE):
                name = search.group(0)
                match = search.group(1)
                if match.lower() == "son":
                    call_cls = SonarrAPI
                elif match.lower() == "anim":
                    call_cls = SonarrAPI
                elif match.lower() == "rad":
                    call_cls = RadarrAPI
                else:
                    call_cls = None
                try:
                    managed_object = Arr(name, self, client_cls=call_cls)
                    self.groups.add(name)
                    self.uris.add(managed_object.uri)
                    self.managed_objects[managed_object.category] = managed_object
                except KeyError as e:
                    self.logger.critical(e)
                except ValueError as e:
                    self.logger.exception(e)
                except SkipException:
                    continue
                except (OSError, TypeError) as e:
                    self.logger.exception(e)
        for cat in self.special_categories:
            managed_object = PlaceHolderArr(cat, self)
            self.managed_objects[cat] = managed_object
        return self
