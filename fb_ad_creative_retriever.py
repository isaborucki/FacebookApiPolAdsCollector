"""Module to coordinate retrieval, processing, and storage of Facebook ad creatives dta.

**Unfortunately this requires access to a private repo**

1. Clone project facebook-ad-scraper
2. Build python package fbactiveads (instructions in setup.py)
3. Install package built in previous step with pip (pip install path/to/package)
4. Install package's dependencies (pip install -r path/to/fbactiveads/requirements.txt)
"""
import collections
import datetime
import enum
import hashlib
import logging
import io
import os.path
import socket
import sys
import time

import dhash
from google.cloud import storage
from langdetect import detect
from langdetect import DetectorFactory
from langdetect.lang_detect_exception import LangDetectException
import requests
from PIL import Image
import tenacity

from fbactiveads.adsnapshots import ad_creative_retriever
from fbactiveads.adsnapshots import browser_context
from fbactiveads.common import config as fbactiveads_config
from fbactiveads.common.crawler import EndBatchCrawlerException
from selenium.common.exceptions import WebDriverException

import config_utils
import db_functions
import sim_hash_ad_creative_text
import slack_notifier


LOGGER = logging.getLogger(__name__)
AD_CREATIVE_IMAGES_BUCKET = 'facebook_ad_images'
AD_CREATIVE_VIDEOS_BUCKET = 'facebook_ad_videos'
ARCHIVE_SCREENSHOTS_BUCKET = 'facebook_ad_archive_screenshots'
CONTENT_LENGTH_HEADER = 'content-length'
GCS_CREDENTIALS_FILE = 'gcs_credentials.json'
VIDEO_HASH_PATH_DIR_NAME_LENGTH = 4
DEFAULT_MAX_VIDEO_DOWNLOAD_SIZE = 512000000 # approx 512 MB
DEFAULT_MAX_ARCHIVE_IDS = 200
DEFAULT_BATCH_SIZE = 20
RESET_BROWSER_AFTER_PROCESSING_N_SNAPSHOTS = 2000
TOO_MANY_REQUESTS_SLEEP_TIME = 4 * 60 * 60 # 4 hours
NO_AVAILABLE_WORK_SLEEP_TIME = 1 * 60 * 60 # 1 hour

AdCreativeRecord = collections.namedtuple('AdCreativeRecord', [
    'archive_id',
    'ad_creative_body',
    'ad_creative_body_language',
    'ad_creative_link_url',
    'ad_creative_link_caption',
    'ad_creative_link_title',
    'ad_creative_link_description',
    'ad_creative_link_button_text',
    'text_sha256_hash',
    'image_sha256_hash',
    'image_downloaded_url',
    'image_bucket_path',
    'text_sim_hash',
    'image_sim_hash',
    'video_sha256_hash',
    'video_downloaded_url',
    'video_bucket_path',
])

AdCreativeRecordUniqueConstraintAttributes = collections.namedtuple(
    'AdCreativeRecordUniqueConstraintAttributes',
    ['archive_id',
     'text_sha256_hash',
     'image_sha256_hash',
     'video_sha256_hash'
    ])

AdSnapshotMetadataRecord = collections.namedtuple('AdSnapshotMetadataRecord', [
    'archive_id',
    'snapshot_fetch_time',
    'snapshot_fetch_status'
    ])

DownloadedVideoAttributes = collections.namedtuple('DownloadedVideoAttributes',
                                                   ['video_sha256_hash',
                                                    'video_bucket_path'])


class Error(Exception):
    """Generic error type for this module."""


@enum.unique
class SnapshotFetchStatus(enum.IntEnum):
    UNKNOWN = 0
    SUCCESS = 1
    NO_CONTENT_FOUND = 2
    INVALID_ID_ERROR = 3
    AGE_RESTRICTION_ERROR = 4
    NO_AD_CREATIVES_FOUND = 5
    INTELLECTUAL_PROPERTY_VIOLATION_ERROR = 6
    SNAPSHOT_PERMANENTLY_UNAVAILABLE_ERROR = 7


def chunks(original_list, chunk_size):
    """Yield successive chunks (of size chunk_size) from original_list."""
    for i in range(0, len(original_list), chunk_size):
        yield original_list[i:i + chunk_size]


def make_gcs_bucket_client(bucket_name, credentials_file):
    storage_client = storage.Client.from_service_account_json(credentials_file)
    bucket_client = storage_client.get_bucket(bucket_name)
    return bucket_client


def make_image_hash_file_path(image_hash):
    base_file_name = '%s.jpg' % image_hash
    return os.path.join(image_hash[:4], image_hash[4:8], image_hash[8:12],
                        image_hash[12:16], image_hash[16:20], image_hash[20:24],
                        image_hash[24:28], base_file_name)

def make_video_sha256_hash_file_path(video_sha256_hash):
    base_file_name = '%s.mp4' % video_sha256_hash
    dirs = [video_sha256_hash[x:x + VIDEO_HASH_PATH_DIR_NAME_LENGTH]
            for x in range(0, len(video_sha256_hash) - VIDEO_HASH_PATH_DIR_NAME_LENGTH,
                           VIDEO_HASH_PATH_DIR_NAME_LENGTH)]
    return os.path.join(*dirs, base_file_name)


def get_image_dhash(image_bytes):
    image_file = io.BytesIO(image_bytes)
    image = Image.open(image_file)
    dhash.force_pil()
    row, col = dhash.dhash_row_col(image)
    image_dhash = dhash.format_hex(row, col)
    return image_dhash

@tenacity.retry(stop=tenacity.stop_after_attempt(4),
                wait=tenacity.wait_random_exponential(multiplier=1, max=30),
                before_sleep=tenacity.before_sleep_log(LOGGER, logging.INFO))
def upload_blob(bucket_client, blob_path, blob_data):
    blob = bucket_client.blob(blob_path)
    if blob.exists():
        logging.debug('Blob path %s already exists, skipping upload', blob_path)
    else:
        blob.upload_from_string(blob_data)
    return blob.id

def send_slack_message(slack_url, msg, slack_user_id_to_include=None):
    if slack_user_id_to_include:
        msg = '<@{}> {}'.format(slack_user_id_to_include, msg)
    slack_notifier.notify_slack(slack_url, msg)


class FacebookAdCreativeRetriever:

    def __init__(self, database_connection_params, creative_retriever_factory,
                 browser_context_factory, ad_creative_images_bucket_client,
                 ad_creative_videos_bucket_client, archive_screenshots_bucket_client,
                 commit_to_db_every_n_processed, slack_url, slack_user_id_to_include,
                 max_video_download_size=DEFAULT_MAX_VIDEO_DOWNLOAD_SIZE):
        self.ad_creative_images_bucket_client = ad_creative_images_bucket_client
        self.ad_creative_videos_bucket_client = ad_creative_videos_bucket_client
        self.archive_screenshots_bucket_client = archive_screenshots_bucket_client
        self.max_video_download_size = max_video_download_size
        self.num_snapshots_processed = 0
        self.num_snapshots_fetch_failed = 0
        self.num_ad_creatives_found = 0
        self.num_snapshots_without_creative_found = 0
        self.num_image_download_success = 0
        self.num_image_download_failure = 0
        self.num_image_uploade_to_gcs_bucket = 0
        self.num_video_download_success = 0
        self.num_video_download_failure = 0
        self.num_video_uploade_to_gcs_bucket = 0
        self.current_batch_id = None
        self.database_connection_params = database_connection_params
        self.commit_to_db_every_n_processed = commit_to_db_every_n_processed
        self.start_time = None
        self.slack_url = slack_url
        self.slack_user_id_to_include = slack_user_id_to_include
        self.creative_retriever_factory = creative_retriever_factory
        self.browser_context_factory = browser_context_factory
        self.creative_retriever_generator = self.make_creative_retriever_generator()
        self.creative_retriever = None

    def get_seconds_elapsed_procesing(self):
        if not self.start_time:
            return 0

        return time.monotonic() - self.start_time

    def reset_start_time(self):
        self.start_time = time.monotonic()

    def log_stats(self):
        seconds_elapsed_procesing = self.get_seconds_elapsed_procesing()
        logging.info(
            'Processed %d archive snapshots in %d seconds.\n'
            'Failed to fetch %d archive snapshots.\n'
            'Ad creatives found: %d\n'
            'Archive snapshots without ad creative found: %d\n'
            'Images downloads successful: %d\n'
            'Images downloads failed: %d\n'
            'Images uploaded to GCS bucket: %d\n'
            'Videos downloads successful: %d\n'
            'Videos downloads failed: %d\n'
            'Videos uploaded to GCS bucket: %d\n'
            'Average time spent per ad creative: %f seconds\n'
            'Current batch ID: %s',
            self.num_snapshots_processed, seconds_elapsed_procesing,
            self.num_snapshots_fetch_failed, self.num_ad_creatives_found,
            self.num_snapshots_without_creative_found,
            self.num_image_download_success,
            self.num_image_download_failure,
            self.num_image_uploade_to_gcs_bucket,
            self.num_video_download_success,
            self.num_video_download_failure,
            self.num_video_uploade_to_gcs_bucket,
            seconds_elapsed_procesing / (self.num_ad_creatives_found or 1),
            self.current_batch_id)

    def get_archive_id_batch_or_wait_until_available(self):
        """Get batch of archive IDs to fetch. Block until results are available."""
        while True:
            with db_functions.db_interface_context(self.database_connection_params) as db_interface:
                batch_and_archive_ids = db_interface.get_archive_id_batch_to_fetch()

            if batch_and_archive_ids:
                return batch_and_archive_ids

            logging.info('No work available right now. Sleeping %s seconds',
                         NO_AVAILABLE_WORK_SLEEP_TIME)
            time.sleep(NO_AVAILABLE_WORK_SLEEP_TIME)
            self.reset_start_time()

    def make_creative_retriever_generator(self):
        """Generator to encapsulate creation of new ad_creative_retriever instances. On each
        interation the old browser context and creative retriever instance are release, a new
        browser context is created, and a new retriever instance is yielded.
        """
        while True:
            with self.browser_context_factory.web_browser() as browser:
                new_creative_retriever = self.creative_retriever_factory.build(
                    chrome_driver=browser)
                yield new_creative_retriever

    def reset_creative_retriever(self):
        logging.info('Resetting creative retriever instance')
        self.creative_retriever = next(self.creative_retriever_generator)


    def retreive_and_store_ad_creatives(self):
        logging.info('Max video download size %d bytes', self.max_video_download_size)
        self.reset_creative_retriever()
        self.reset_start_time()
        num_snapshots_processed_since_chromedriver_reset = 0
        while True:
            try:
                batch_and_archive_ids = self.get_archive_id_batch_or_wait_until_available()
                self.current_batch_id = batch_and_archive_ids['batch_id']
                archive_id_batch = batch_and_archive_ids['archive_ids']
                logging.info(
                    'Processing batch ID %d of %d archive snapshots in chunks of %d',
                    self.current_batch_id, len(archive_id_batch),
                    self.commit_to_db_every_n_processed)
                try:
                    num_snapshots_processed_in_current_batch = 0
                    for archive_id_chunk in chunks(archive_id_batch,
                                                   self.commit_to_db_every_n_processed):
                        self.process_archive_ids(archive_id_chunk)
                        #  self.db_connection.commit()
                        num_snapshots_processed_in_current_batch += len(archive_id_chunk)
                        logging.info('Processed %d of %d archive snapshots.',
                                     num_snapshots_processed_in_current_batch,
                                     len(archive_id_batch))
                        self.log_stats()
                except BaseException as error:
                    logging.info(
                        'Releasing snapshot_fetch_batch_id %s due to unhandled exception: '
                        '%s', self.current_batch_id, error)
                    with db_functions.db_interface_context(self.database_connection_params) \
                        as db_interface:
                        db_interface.release_uncompleted_fetch_batch(self.current_batch_id)
                    raise
                num_snapshots_processed_since_chromedriver_reset += len(archive_id_batch)

                with db_functions.db_interface_context(self.database_connection_params) \
                    as db_interface:
                    db_interface.mark_fetch_batch_completed(self.current_batch_id)

                if (num_snapshots_processed_since_chromedriver_reset >=
                    RESET_BROWSER_AFTER_PROCESSING_N_SNAPSHOTS):
                    logging.info('Processed %d snapshots since last reset (limit: %d)',
                                 num_snapshots_processed_since_chromedriver_reset,
                                 RESET_BROWSER_AFTER_PROCESSING_N_SNAPSHOTS)
                    self.reset_creative_retriever()
                    num_snapshots_processed_since_chromedriver_reset = 0

            except (ad_creative_retriever.TooManyRequestsError, EndBatchCrawlerException) as error:
                suggested_sleep_time = getattr(error, 'wait_before_next_batch_seconds',
                                               TOO_MANY_REQUESTS_SLEEP_TIME)
                slack_msg = (
                    ':rotating_light: :rotating_light: :rotating_light: '
                    'fb_ad_creative_retriever.py raised %r on host %s. Sleeping %d seconds! '
                    ':rotating_light: :rotating_light: :rotating_light:' % (
                        error, socket.getfqdn(), suggested_sleep_time))
                send_slack_message(self.slack_url, slack_msg,
                                   slack_user_id_to_include=self.slack_user_id_to_include)
                logging.error('%s raised. Sleeping %d seconds.',
                              error, suggested_sleep_time)
                time.sleep(suggested_sleep_time)
            finally:
                self.log_stats()

    def store_image_in_google_bucket(self, image_dhash, image_bytes):
        image_bucket_path = make_image_hash_file_path(image_dhash)
        blob_id = upload_blob(self.ad_creative_images_bucket_client, image_bucket_path, image_bytes)
        self.num_image_uploade_to_gcs_bucket += 1
        logging.debug('Image dhash: %s; uploaded to: %s', image_dhash, blob_id)
        return image_bucket_path

    def store_video_in_google_bucket(self, video_sha256_hash, video_bytes):
        video_bucket_path = make_video_sha256_hash_file_path(video_sha256_hash)
        blob_id = upload_blob(self.ad_creative_videos_bucket_client, video_bucket_path, video_bytes)
        self.num_video_uploade_to_gcs_bucket += 1
        logging.debug('Video sha256_hash: %s; uploaded to: %s', video_sha256_hash, blob_id)
        return video_bucket_path

    def store_snapshot_screenshot(self, archive_id, screenshot_binary_data):
        bucket_path = '%d.png' % archive_id
        blob_id = upload_blob(self.archive_screenshots_bucket_client, bucket_path,
                              screenshot_binary_data)
        logging.debug('Uploaded %d archive_id snapshot to %s', archive_id, blob_id)

    def retrieve_ad(self, archive_id):
        snapshot_fetch_status = SnapshotFetchStatus.UNKNOWN
        screenshot_and_creatives = None
        try:
            logging.info('Retrieving creatives for archive ID %s', archive_id)
            try:
                fetch_time = datetime.datetime.now()
                screenshot_and_creatives = self.creative_retriever.retrieve_ad(str(archive_id))
            except (ad_creative_retriever.BrowserTimeoutError, WebDriverException) as error:
                logging.info('Browser error (%s), resetting ad creative retriever', error)
                self.reset_creative_retriever()
                fetch_time = datetime.datetime.now()
                screenshot_and_creatives = self.creative_retriever.retrieve_ad(str(archive_id))

            logging.debug('%s creatives:\n%s', archive_id, screenshot_and_creatives.creatives)

            if screenshot_and_creatives.creatives:
                snapshot_fetch_status = SnapshotFetchStatus.SUCCESS
            else:
                snapshot_fetch_status = SnapshotFetchStatus.NO_AD_CREATIVES_FOUND
                logging.info(
                    'Unable to find ad creative(s) for archive_id: %s', archive_id)

        except requests.RequestException as request_exception:
            logging.info(
                'Request exception while processing archive id:%s\n%s',
                archive_id, request_exception)
            self.num_snapshots_fetch_failed += 1
            # TODO(macpd): decide how to count the errors below
        except (ad_creative_retriever.SnapshotNoContentFoundError,
                ad_creative_retriever.SnapshotMissingMediaError):
            logging.info('No content found for archive_id %d', archive_id)
            snapshot_fetch_status = SnapshotFetchStatus.NO_CONTENT_FOUND
        except ad_creative_retriever.SnapshotAgeRestrictionError:
            snapshot_fetch_status = SnapshotFetchStatus.AGE_RESTRICTION_ERROR
        except ad_creative_retriever.SnapshotIntellectualPropertyViolationError:
            snapshot_fetch_status = SnapshotFetchStatus.INTELLECTUAL_PROPERTY_VIOLATION_ERROR
        except (ad_creative_retriever.SnapshotInvalidIdError,
                ad_creative_retriever.SnapshotWrongAdArchiveIdError):
            snapshot_fetch_status = SnapshotFetchStatus.INVALID_ID_ERROR
        except ad_creative_retriever.SnapshotPermanentlyUnavailableError:
            snapshot_fetch_status = SnapshotFetchStatus.SNAPSHOT_PERMANENTLY_UNAVAILABLE_ERROR

        # TODO(macpd): use ad_creative_retriever errors and exceptions
        snapshot_metadata_record = AdSnapshotMetadataRecord(
            archive_id=archive_id, snapshot_fetch_time=fetch_time,
            snapshot_fetch_status=snapshot_fetch_status)
        self.num_snapshots_processed += 1

        return screenshot_and_creatives, snapshot_metadata_record

    def process_archive_ids(self, archive_ids):
        archive_ids_without_creative_found = 0
        snapshot_metadata_records = []
        ad_creative_records = []
        for archive_id in archive_ids:
            screenshot_and_creatives, snapshot_metadata_record = self.retrieve_ad(archive_id)
            snapshot_metadata_records.append(snapshot_metadata_record)
            if not screenshot_and_creatives:
                archive_ids_without_creative_found += 1
                logging.info(
                    'Unable to get screenshot or creative(s) for archive_id: %s', archive_id)
                continue

            if screenshot_and_creatives.screenshot_binary_data:
                self.store_snapshot_screenshot(archive_id,
                                               screenshot_and_creatives.screenshot_binary_data)
            else:
                logging.info('No screenshot for archive ID: %s', archive_id)

            if screenshot_and_creatives.creatives:
                new_ad_creative_recoreds = self.process_fetched_ad_creative_data(
                    archive_id, screenshot_and_creatives)
                if new_ad_creative_recoreds:
                    ad_creative_records.extend(new_ad_creative_recoreds)
                else:
                    logging.info('No ad creative records generated for archive ID: %s', archive_id)
            else:
                archive_ids_without_creative_found += 1
                logging.info(
                    'Unable to find ad creative(s) for archive_id: %s', archive_id)

        self.num_ad_creatives_found += len(ad_creative_records)
        self.num_snapshots_without_creative_found += archive_ids_without_creative_found

        logging.info('Inserting %d AdCreativeRecords to to DB.',
                     len(ad_creative_records))
        logging.debug('Inserting AdCreativeRecords to DB: %r',
                      ad_creative_records)

        with db_functions.db_interface_context(self.database_connection_params) as db_interface:
            db_interface.insert_ad_creative_records(ad_creative_records)
            logging.info('Updating %d snapshot metadata records.', len(snapshot_metadata_records))
            db_interface.update_ad_snapshot_metadata(snapshot_metadata_records)

    def download_video(self, archive_id, video_url):
        video_bytes = None
        try:
            with requests.get(video_url, timeout=30, stream=True) as video_request:
                # TODO(macpd): handle this more gracefully
                # TODO(macpd): check encoding
                video_request.raise_for_status()
                if CONTENT_LENGTH_HEADER not in video_request.headers:
                    logging.info('Refusing to download video for %s, no \'%s\' header in response.',
                                 archive_id, CONTENT_LENGTH_HEADER)
                    return None

                try:
                    video_content_len = int(video_request.headers.get(CONTENT_LENGTH_HEADER))
                except ValueError:
                    logging.info(
                        'Unable to convert %s header value (%s) to int for archive ID '
                        '%s. Refusing to download video.', CONTENT_LENGTH_HEADER,
                        video_request.headers.get(CONTENT_LENGTH_HEADER),
                        archive_id)
                    return None

                if video_content_len > self.max_video_download_size:
                    logging.info(
                        '%s video size (%s bytes) exceeds max_video_download_size %s',
                        archive_id, video_request.headers['content-length'],
                        self.max_video_download_size)
                    self.num_video_download_failure += 1
                    return None

                video_bytes = video_request.content

        except requests.RequestException as request_exception:
            logging.info('Exception %s when requesting video_url: %s',
                         request_exception, video_url)
            self.num_video_download_failure += 1
            # TODO(macpd): handle all error types
            return None

        self.num_video_download_success += 1
        video_sha256 = hashlib.sha256(video_bytes).hexdigest()
        video_bucket_path = self.store_video_in_google_bucket(
            video_sha256, video_bytes)
        return DownloadedVideoAttributes(video_sha256_hash=video_sha256,
                                         video_bucket_path=video_bucket_path)


    def process_fetched_ad_creative_data(self, archive_id, fetched_data):
        if not fetched_data.creatives:
            logging.warning('No creatives for %s', archive_id)
            return None

        # Used to prevent sending multiple records for upsert in same batch that have duplicate
        # attributes the database requires to be unique.
        seen_unique_constraint_attrs = set()
        ad_creative_records = []

        for creative in fetched_data.creatives:
            image_dhash = None
            image_sha256 = None
            image_bucket_path = None
            image_url = None
            video_sha256 = None
            video_bucket_path = None
            if creative.image:
                try:
                    image_dhash = get_image_dhash(creative.image.binary_data)
                except OSError as error:
                    logging.warning(
                        "Error generating dhash for archive ID: %s, image_url: %s. "
                        "images_bytes len: %d\n%s", archive_id,
                        creative.image.url, len(creative.image.binary_data), error)
                    self.num_image_download_failure += 1
                    continue

                self.num_image_download_success += 1
                image_url = creative.image.url
                image_sha256 = hashlib.sha256(creative.image.binary_data).hexdigest()
                image_bucket_path = self.store_image_in_google_bucket(
                    image_dhash, creative.image.binary_data)
            if creative.video_url:
                downloaded_video_attributes = self.download_video(archive_id, creative.video_url)
                if downloaded_video_attributes:
                    video_sha256 = downloaded_video_attributes.video_sha256_hash
                    video_bucket_path = downloaded_video_attributes.video_bucket_path

            text = None
            text_sim_hash = None
            text_sha256_hash = None
            ad_creative_body_language = None
            if creative.body:
                text = creative.body
                # Get simhash as hex without leading '0x'
                text_sim_hash = '%x' % sim_hash_ad_creative_text.hash_ad_creative_text(
                    text)
                text_sha256_hash = hashlib.sha256(bytes(
                    text, encoding='UTF-32')).hexdigest()
                try:
                    ad_creative_body_language = detect(text)
                except LangDetectException as error:
                    logging.info('Unable to determine language of ad creative body from %s',
                                 archive_id)
                    ad_creative_body_language = None

            unique_constraint_attrs = AdCreativeRecordUniqueConstraintAttributes(
                archive_id=archive_id, text_sha256_hash=text_sha256_hash,
                image_sha256_hash=image_sha256, video_sha256_hash=video_sha256)

            if unique_constraint_attrs in seen_unique_constraint_attrs:
                logging.info(
                    'Dropping ad record with duplicate unique constriant attributes: %s',
                    unique_constraint_attrs)
                continue

            ad_creative_link_url = None
            ad_creative_link_caption = None
            ad_creative_link_title = None
            ad_creative_link_description = None
            ad_creative_link_button_text = None

            if creative.link_attributes:
                ad_creative_link_url = creative.link_attributes.url
                ad_creative_link_caption = creative.link_attributes.caption
                ad_creative_link_title = creative.link_attributes.title
                ad_creative_link_description = creative.link_attributes.description
                ad_creative_link_button_text = creative.link_attributes.button

            seen_unique_constraint_attrs.add(unique_constraint_attrs)
            ad_creative_records.append(
                AdCreativeRecord(
                    ad_creative_body=text,
                    ad_creative_body_language=ad_creative_body_language,
                    ad_creative_link_url=ad_creative_link_url,
                    ad_creative_link_caption=ad_creative_link_caption,
                    ad_creative_link_title=ad_creative_link_title,
                    ad_creative_link_description=ad_creative_link_description,
                    ad_creative_link_button_text=ad_creative_link_button_text,
                    archive_id=archive_id,
                    text_sha256_hash=text_sha256_hash,
                    text_sim_hash=text_sim_hash,
                    image_downloaded_url=image_url,
                    image_bucket_path=image_bucket_path,
                    image_sim_hash=image_dhash,
                    image_sha256_hash=image_sha256,
                    video_downloaded_url=creative.video_url,
                    video_bucket_path=video_bucket_path,
                    video_sha256_hash=video_sha256))

        return ad_creative_records


def main(argv):
    config = fbactiveads_config.load_config(argv[0])

    # Force consistent langdetect results. https://pypi.org/project/langdetect/
    DetectorFactory.seed = 0

    commit_to_db_every_n_processed = config.getint('LIMITS', 'BATCH_SIZE',
                                                   fallback=DEFAULT_BATCH_SIZE)
    logging.info('Will commit to DB every %d snapshots processed.', commit_to_db_every_n_processed)
    slack_url = config.get('LOGGING', 'SLACK_URL')
    slack_user_id_to_include = config.get('LOGGING', 'SLACK_USER_ID_TO_INCLUDE', fallback=None)
    max_video_download_size = config.getint('LIMITS', 'max_video_download_size',
                                            fallback=DEFAULT_MAX_VIDEO_DOWNLOAD_SIZE)

    database_connection_params = config_utils.get_database_connection_params_from_config(config)
    creative_retriever_factory = ad_creative_retriever.FacebookAdCreativeRetrieverFactory(config)
    browser_context_factory = browser_context.DockerSeleniumBrowserContextFactory(config)

    ad_creative_images_bucket_client = make_gcs_bucket_client(AD_CREATIVE_IMAGES_BUCKET,
                                                              GCS_CREDENTIALS_FILE)
    ad_creative_video_bucket_client = make_gcs_bucket_client(AD_CREATIVE_VIDEOS_BUCKET,
                                                             GCS_CREDENTIALS_FILE)
    archive_screenshots_bucket_client = make_gcs_bucket_client(ARCHIVE_SCREENSHOTS_BUCKET,
                                                               GCS_CREDENTIALS_FILE)
    image_retriever = FacebookAdCreativeRetriever(
        database_connection_params, creative_retriever_factory, browser_context_factory,
        ad_creative_images_bucket_client, ad_creative_video_bucket_client,
        archive_screenshots_bucket_client, commit_to_db_every_n_processed, slack_url,
        slack_user_id_to_include, max_video_download_size=max_video_download_size)
    try:
        image_retriever.retreive_and_store_ad_creatives()
    except KeyboardInterrupt:
        # don't log about Ctrl-C
        raise
    except BaseException as error:
        slack_msg = (
            ':rotating_light: :rotating_light: :rotating_light: '
            'fb_ad_creative_retriever.py raised |%r| on host %s.'
            ':rotating_light: :rotating_light: :rotating_light:' % (
                error, socket.getfqdn()))
        send_slack_message(slack_url, slack_msg,
                           slack_user_id_to_include=slack_user_id_to_include)
        raise


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit('Usage: %s <config file>' % sys.argv[0])
    config_utils.configure_logger("fb_ad_creative_retriever.log")
    main(sys.argv[1:])
