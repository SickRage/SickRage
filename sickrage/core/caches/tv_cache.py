# Author: echel0n <echel0n@sickrage.ca>
# URL: https://sickrage.ca
#
# This file is part of SiCKRAGE.
#
# SiCKRAGE is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SiCKRAGE is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with SiCKRAGE.  If not, see <http://www.gnu.org/licenses/>.


import datetime
import time

import feedparser
from sqlalchemy import orm

import sickrage
from sickrage.core.api.cache import ProviderCacheAPI
from sickrage.core.common import Quality
from sickrage.core.databases.cache import CacheDB
from sickrage.core.exceptions import AuthException, EpisodeNotFoundException
from sickrage.core.helpers import show_names, validate_url, is_ip_private, try_int
from sickrage.core.tv.episode.helpers import find_episode
from sickrage.core.tv.show.helpers import find_show
from sickrage.core.nameparser import InvalidNameException, NameParser, InvalidShowException
from sickrage.core.websession import WebSession


class TVCache(object):
    def __init__(self, provider, **kwargs):
        self.provider = provider
        self.providerID = self.provider.id
        self.min_time = kwargs.pop('min_time', 10)
        self.search_strings = kwargs.pop('search_strings', dict(RSS=['']))

    @CacheDB.with_session
    def clear(self, session=None):
        if self.shouldClearCache():
            session.query(CacheDB.Provider).filter_by(provider=self.providerID).delete()

    def _get_title_and_url(self, item):
        return self.provider._get_title_and_url(item)

    def _get_result_stats(self, item):
        return self.provider._get_result_stats(item)

    def _get_size(self, item):
        return self.provider._get_size(item)

    def _get_rss_data(self):
        if self.search_strings:
            return {'entries': self.provider.search(self.search_strings)}

    def _check_auth(self, data):
        return True

    def check_item(self, title, url):
        return True

    def update(self, force=False):
        # check if we should update
        if self.should_update() or force:
            try:
                data = self._get_rss_data()
                if not self._check_auth(data):
                    return False

                # clear cache
                self.clear()

                # set updated
                self.last_update = datetime.datetime.today()

                [self._parseItem(item) for item in data['entries']]
            except AuthException as e:
                sickrage.app.log.warning("Authentication error: {}".format(e))
                return False
            except Exception as e:
                sickrage.app.log.debug(
                    "Error while searching {}, skipping: {}".format(self.provider.name, repr(e)))
                return False

        return True

    def get_rss_feed(self, url, params=None):
        try:
            if self.provider.login():
                resp = WebSession().get(url, params=params).text
                return feedparser.parse(resp)
        except Exception as e:
            sickrage.app.log.debug("RSS Error: {}".format(e))

        return feedparser.FeedParserDict()

    def _translateTitle(self, title):
        return '' + title.replace(' ', '.')

    def _translateLinkURL(self, url):
        return url.replace('&amp;', '&')

    def _parseItem(self, item):
        title, url = self._get_title_and_url(item)
        seeders, leechers = self._get_result_stats(item)
        size = self._get_size(item)

        self.check_item(title, url)

        if title and url:
            self.add_cache_entry(self._translateTitle(title), self._translateLinkURL(url), seeders, leechers, size)
        else:
            sickrage.app.log.debug(
                "The data returned from the " + self.provider.name + " feed is incomplete, this result is unusable")

    @property
    def last_update(self):
        try:
            dbData = CacheDB.LastUpdate.query.filter_by(provider=self.providerID).one()
            lastTime = int(dbData.time)
            if lastTime > int(time.mktime(datetime.datetime.today().timetuple())):
                lastTime = 0
        except orm.exc.NoResultFound:
            lastTime = 0

        return datetime.datetime.fromtimestamp(lastTime)

    @last_update.setter
    @CacheDB.with_session
    def last_update(self, toDate, session=None):
        try:
            dbData = session.query(CacheDB.LastUpdate).filter_by(provider=self.providerID).one()
            dbData.time = int(time.mktime(toDate.timetuple()))
        except orm.exc.NoResultFound:
            session.add(CacheDB.LastUpdate(**{
                'provider': self.providerID,
                'time': int(time.mktime(toDate.timetuple()))
            }))

    @property
    @CacheDB.with_session
    def last_search(self, session=None):
        try:
            dbData = session.query(CacheDB.LastSearch).filter_by(provider=self.providerID).one()
            lastTime = int(dbData.time)
            if lastTime > int(time.mktime(datetime.datetime.today().timetuple())):
                lastTime = 0
        except orm.exc.NoResultFound:
            lastTime = 0

        return datetime.datetime.fromtimestamp(lastTime)

    @last_search.setter
    @CacheDB.with_session
    def last_search(self, toDate, session=None):
        try:
            dbData = session.query(CacheDB.LastSearch).filter_by(provider=self.providerID).one()
            dbData.time = int(time.mktime(toDate.timetuple()))
        except orm.exc.NoResultFound:
            session.add(CacheDB.LastSearch(**{
                'provider': self.providerID,
                'time': int(time.mktime(toDate.timetuple()))
            }))

    def should_update(self):
        # if we've updated recently then skip the update
        if datetime.datetime.today() - self.last_update < datetime.timedelta(minutes=self.min_time):
            return False
        return True

    def shouldClearCache(self):
        # if daily search hasn't used our previous results yet then don't clear the cache
        if self.last_update > self.last_search:
            return False
        return True

    @CacheDB.with_session
    def add_cache_entry(self, name, url, seeders, leechers, size, session=None):
        # check for existing entry in cache
        if session.query(CacheDB.Provider).filter_by(provider=self.providerID, url=url).one_or_none():
            return

        # ignore invalid and private IP address urls
        if not validate_url(url):
            if not url.startswith('magnet'):
                return
        elif is_ip_private(url.split(r'//')[-1].split(r'/')[0]):
            return

        try:
            # parse release name
            parse_result = NameParser(validate_show=True).parse(name)
            if parse_result.series_name and parse_result.quality != Quality.UNKNOWN:
                season = parse_result.season_number if parse_result.season_number else 1
                episodes = parse_result.episode_numbers

                if season and episodes:
                    # store episodes as a seperated string
                    episodeText = "|" + "|".join(map(str, episodes)) + "|"

                    # get quality of release
                    quality = parse_result.quality

                    # get release group
                    release_group = parse_result.release_group

                    # get version
                    version = parse_result.version

                    dbData = {
                        'provider': self.providerID,
                        'name': name,
                        'season': season,
                        'episodes': episodeText,
                        'indexer_id': parse_result.indexer_id,
                        'url': url,
                        'time': int(time.mktime(datetime.datetime.today().timetuple())),
                        'quality': quality,
                        'release_group': release_group,
                        'version': version,
                        'seeders': try_int(seeders),
                        'leechers': try_int(leechers),
                        'size': try_int(size, -1)
                    }

                    # add to internal database
                    session.add(CacheDB.Provider(**dbData))

                    # add to external provider cache database
                    if sickrage.app.config.enable_api_providers_cache and not self.provider.private:
                        try:
                            sickrage.app.event_queue.fire_event(ProviderCacheAPI().add, data=dbData)
                        except Exception:
                            pass

                    sickrage.app.log.debug("SEARCH RESULT:[{}] ADDED TO CACHE!".format(name))
        except (InvalidShowException, InvalidNameException):
            pass

    @CacheDB.with_session
    def search_cache(self, show_id, episode_id, manualSearch=False, downCurQuality=False, session=None):
        episode_obj = find_episode(show_id, episode_id)

        season = episode_obj.scene_season if episode_obj.show.scene else episode_obj.season
        episode = episode_obj.scene_episode if episode_obj.show.scene else episode_obj.episode

        neededEps = {}
        dbData = []

        # get data from external database
        if sickrage.app.config.enable_api_providers_cache and not self.provider.private:
            try:
                dbData += ProviderCacheAPI().get(self.providerID, show_id, season, episode)['data']
            except Exception:
                pass

        # get data from internal database
        dbData += [
            x.as_dict() for x in session.query(CacheDB.Provider).filter_by(provider=self.providerID, indexer_id=show_id, season=season) if
            "|{}|".format(episode) in x.episodes
        ]

        # for each cache entry
        for curResult in dbData:
            show = find_show(int(curResult["indexer_id"]))

            result = self.provider.getResult()

            # ignore invalid and private IP address urls
            if not validate_url(curResult["url"]):
                if not curResult["url"].startswith('magnet'):
                    continue
            elif is_ip_private(curResult["url"].split(r'//')[-1].split(r'/')[0]):
                continue

            # ignored/required words, and non-tv junk
            if not show_names.filter_bad_releases(curResult["name"]):
                continue

            # get the show object, or if it's not one of our shows then ignore it
            result.show_id = int(curResult["indexer_id"])

            # skip if provider is anime only and show is not anime
            if self.provider.anime_only and not show.is_anime:
                sickrage.app.log.debug("" + str(show.name) + " is not an anime, skiping")
                continue

            # get season and ep data (ignoring multi-eps for now)
            curSeason = int(curResult["season"])
            if curSeason == -1:
                continue

            try:
                result.episode_ids = [show.get_episode(curSeason, int(curEp)).indexer_id for curEp in filter(None, curResult["episodes"].split("|"))]
            except EpisodeNotFoundException:
                continue

            result.quality = int(curResult["quality"])
            result.release_group = curResult["release_group"]
            result.version = curResult["version"]

            # make sure we want the episode
            wantEp = False
            for episode_id in result.episode_ids:
                if show.want_episode(episode_id, result.quality, manualSearch, downCurQuality):
                    wantEp = True

            if not wantEp:
                sickrage.app.log.info("Skipping " + curResult["name"] + " because we don't want an episode that's " + Quality.qualityStrings[result.quality])
                continue

            # build a result object
            result.name = curResult["name"]
            result.url = curResult["url"]

            sickrage.app.log.info("Found result " + result.name + " at " + result.url)

            result.seeders = curResult.get("seeders", -1)
            result.leechers = curResult.get("leechers", -1)
            result.size = curResult.get("size", -1)
            result.content = None

            # add it to the list
            if episode_id not in neededEps:
                neededEps[episode_id] = [result]
            else:
                neededEps[episode_id] += [result]

        # datetime stamp this search so cache gets cleared
        self.last_search = datetime.datetime.today()

        return neededEps
