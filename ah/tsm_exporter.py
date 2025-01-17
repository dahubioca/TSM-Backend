from typing import List, Set, Optional
import argparse
import logging
import sys
import os

import numpy as np

from ah.models import (
    MapItemStringMarketValueRecords,
    RegionEnum,
    Namespace,
    NameSpaceCategoriesEnum,
    GameVersionEnum,
    DBTypeEnum,
    FactionEnum,
)
from ah.storage import TextFile
from ah.db import AuctionDB
from ah.api import GHAPI
from ah.cache import Cache
from ah import config


class TSMExporter:
    REALM_AUCTIONS_EXPORT = {
        "type": "AUCTIONDB_REALM_DATA",
        "sources": ["realm_auctions"],
        "desc": "realm latest scan data",
        "fields": [
            "itemString",
            "minBuyout",
            "numAuctions",
            "marketValueRecent",
        ],
        "per_faction": True,
    }
    REALM_AUCTIONS_COMMODITIES_EXPORTS = [
        {
            "type": "AUCTIONDB_REALM_HISTORICAL",
            "sources": ["realm_auctions", "commodities"],
            "desc": "realm historical data, realm auction and commodities",
            "fields": [
                "itemString",
                "historical",
            ],
            "per_faction": True,
        },
        {
            "type": "AUCTIONDB_REALM_SCAN_STAT",
            "sources": ["realm_auctions", "commodities"],
            "desc": "realm two week data, realm auction and commodities",
            "fields": [
                "itemString",
                "marketValue",
            ],
            "per_faction": True,
        },
    ]
    COMMODITIES_EXPORT = {
        "type": "AUCTIONDB_REGION_COMMODITY",
        "sources": ["commodities"],
        "desc": "region commodity data",
        "fields": [
            "itemString",
            "minBuyout",
            "numAuctions",
            "marketValueRecent",
        ],
    }
    REGION_AUCTIONS_COMMODITIES_EXPORTS = [
        {
            "type": "AUCTIONDB_REGION_STAT",
            "sources": ["region_auctions", "commodities"],
            "desc": "region two week data, auctions from all realms and commodities",
            "fields": [
                "itemString",
                "regionMarketValue",
            ],
        },
        {
            "type": "AUCTIONDB_REGION_HISTORICAL",
            "sources": ["region_auctions", "commodities"],
            "desc": "region historical data, auctions from all realms and commodities",
            "fields": [
                "itemString",
                "regionHistorical",
            ],
        },
    ]
    TEMPLATE_ROW = (
        'select(2, ...).LoadData("{data_type}","{region_or_realm}",[[return '
        "{{downloadTime={ts},fields={{{fields}}},data={{{data}}}}}]])"
    )
    TEMPLATE_APPDATA = (
        'select(2, ...).LoadData("APP_INFO","Global",[[return '
        "{{version={version},lastSync={last_sync},"
        'message={{id=0,msg=""}},news={{}}}}]])'
    )
    NUMERIC_SET = set("0123456789")
    TSM_VERSION = 41200
    _logger = logging.getLogger("TSMExporter")

    def __init__(self, db: AuctionDB, export_file: TextFile) -> None:
        self.db = db
        self.export_file = export_file

    @classmethod
    def get_tsm_appdata_path(cls, warcraft_path: str) -> str:
        return os.path.join(
            warcraft_path,
            "Interface",
            "AddOns",
            "TradeSkillMaster_AppHelper",
            "AppData.lua",
        )

    @classmethod
    def find_warcraft_base_windows(cls) -> str:
        if sys.platform == "win32":
            import winreg
        else:
            raise RuntimeError("Only support windows")

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft",
        )
        path = winreg.QueryValueEx(key, "InstallPath")[0]
        path = os.path.join(path, "..")
        return os.path.normpath(path)

    @classmethod
    def baseN(cls, num, b, numerals="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        return ((num == 0) and numerals[0]) or (
            cls.baseN(num // b, b, numerals).lstrip(numerals[0]) + numerals[num % b]
        )

    @classmethod
    def export_append_data(
        cls,
        file: TextFile,
        map_records: MapItemStringMarketValueRecords,
        fields: List[str],
        type_: str,
        region_or_realm: str,
        ts_update_begin: int,
        ts_update_end: int,
    ) -> None:
        cls._logger.info(f"Exporting {type_} for {region_or_realm}...")
        items_data = []
        for item_string, records in map_records.items():
            # tsm can handle:
            # 1. numeral itemstring being string
            # 2. 10-based numbers
            item_data = []
            # skip item if all numbers are 0 or None
            is_skip_item = True
            for field in fields:
                if field == "minBuyout":
                    value = records.get_recent_min_buyout(ts_update_begin)
                    if value:
                        is_skip_item = False
                elif field == "numAuctions":
                    value = records.get_recent_num_auctions(ts_update_begin)
                    if value:
                        is_skip_item = False
                elif field == "marketValueRecent":
                    value = records.get_recent_market_value(ts_update_begin)
                    if value:
                        is_skip_item = False
                elif field in ["historical", "regionHistorical"]:
                    value = records.get_historical_market_value(ts_update_end)
                    if value:
                        is_skip_item = False
                elif field in ["marketValue", "regionMarketValue"]:
                    value = records.get_weighted_market_value(ts_update_end)
                    if value:
                        is_skip_item = False
                elif field == "itemString":
                    value = item_string.to_str()
                    if not set(value) < cls.NUMERIC_SET:
                        value = '"' + value + '"'
                else:
                    raise ValueError(f"unsupported field {field}.")

                if isinstance(value, (int, np.int32, np.int64)):
                    value = cls.baseN(value, 32)
                elif isinstance(value, float):
                    value = str(value)
                elif isinstance(value, str):
                    pass
                else:
                    raise ValueError(f"unsupported type {type(value)}")

                item_data.append(value)

            if is_skip_item:
                cls._logger.debug(f"Skip item {item_string} due to no data.")
                continue

            item_text = "{" + ",".join(item_data) + "}"
            items_data.append(item_text)

        fields_str = ",".join('"' + field + '"' for field in fields)
        text_out = cls.TEMPLATE_ROW.format(
            data_type=type_,
            region_or_realm=region_or_realm,
            ts=ts_update_begin,
            fields=fields_str,
            data=",".join(items_data),
        )
        with file.open("a", newline="\n", encoding="utf-8") as f:
            f.write(text_out + "\n")

    def export_region(
        self,
        namespace: Namespace,
        export_realms: Set[str],
    ):
        meta_file = self.db.get_file(namespace, DBTypeEnum.META)
        meta = self.db.load_meta(meta_file)
        if not meta:
            raise ValueError(f"meta file {meta_file} not found or Empty.")
        ts_update_start = meta["update"]["start_ts"]
        ts_update_end = meta["update"]["end_ts"]
        map_crid_connected_realms = meta["connected_realms"]

        # makes sure realm names are all valid
        all_realms = {
            realm
            for connected_realms in map_crid_connected_realms.values()
            for realm in connected_realms
        }
        if not export_realms <= all_realms:
            raise ValueError(f"unavailable realms : {export_realms - all_realms}. ")

        region_auctions_commodities_data = MapItemStringMarketValueRecords()

        if namespace.game_version == GameVersionEnum.RETAIL:
            commodity_file = self.db.get_file(namespace, DBTypeEnum.COMMODITIES)
            commodity_data = self.db.load_db(commodity_file)
        else:
            commodity_file = None
            commodity_data = None

        if commodity_data:
            region_auctions_commodities_data.extend(commodity_data)
            self.export_append_data(
                self.export_file,
                commodity_data,
                self.COMMODITIES_EXPORT["fields"],
                self.COMMODITIES_EXPORT["type"],
                namespace.region.upper(),
                ts_update_start,
                ts_update_end,
            )

        if namespace.game_version == GameVersionEnum.RETAIL:
            factions = [None]
        else:
            factions = [FactionEnum.ALLIANCE, FactionEnum.HORDE]

        for crid, connected_realms in map_crid_connected_realms.items():
            crid = int(crid)
            connected_realms = set(connected_realms)
            # find all realm names we want to export under this connected realm,
            # they share the same auction data
            sub_export_realms = export_realms & connected_realms

            for faction in factions:
                db_file = self.db.get_file(
                    namespace,
                    DBTypeEnum.AUCTIONS,
                    crid=crid,
                    faction=faction,
                )
                auction_data = self.db.load_db(db_file)
                if not auction_data:
                    self._logger.warning(f"no data in {db_file}.")
                    continue

                region_auctions_commodities_data.extend(auction_data)
                if commodity_data:
                    realm_auctions_commodities_data = MapItemStringMarketValueRecords()
                    realm_auctions_commodities_data.extend(commodity_data)
                    realm_auctions_commodities_data.extend(auction_data)
                else:
                    realm_auctions_commodities_data = auction_data

                for realm in sub_export_realms:
                    if faction is None:
                        tsm_realm = realm
                    else:
                        tsm_realm = f"{realm}-{faction.get_full_name()}"

                    self.export_append_data(
                        self.export_file,
                        auction_data,
                        self.REALM_AUCTIONS_EXPORT["fields"],
                        self.REALM_AUCTIONS_EXPORT["type"],
                        tsm_realm,
                        ts_update_start,
                        ts_update_end,
                    )
                    for export_realm in self.REALM_AUCTIONS_COMMODITIES_EXPORTS:
                        self.export_append_data(
                            self.export_file,
                            realm_auctions_commodities_data,
                            export_realm["fields"],
                            export_realm["type"],
                            tsm_realm,
                            ts_update_start,
                            ts_update_end,
                        )

        if region_auctions_commodities_data:
            for region_export in self.REGION_AUCTIONS_COMMODITIES_EXPORTS:
                if (
                    namespace.game_version
                    in (
                        GameVersionEnum.CLASSIC,
                        GameVersionEnum.CLASSIC_WLK,
                    )
                    and namespace.region == RegionEnum.TW
                ):
                    # TSM reconizes TW as KR in classic
                    region = "KR"
                else:
                    region = namespace.region.upper()

                tsm_game_version = namespace.game_version.get_tsm_game_version()
                if tsm_game_version:
                    tsm_region = f"{tsm_game_version}-{region}"
                else:
                    tsm_region = region

                self.export_append_data(
                    self.export_file,
                    region_auctions_commodities_data,
                    region_export["fields"],
                    region_export["type"],
                    tsm_region,
                    ts_update_start,
                    ts_update_end,
                )

        self.export_append_app_info(self.export_file, self.TSM_VERSION, ts_update_end)

    @classmethod
    def export_append_app_info(cls, file: TextFile, version: int, ts_last_sync: int):
        # mine windows uses cp936, let's be more explicit here
        # https://docs.python.org/3.10/library/functions.html#open
        with file.open("a", newline="\n", encoding="utf-8") as f:
            text_out = cls.TEMPLATE_APPDATA.format(
                version=version,
                last_sync=ts_last_sync,
            )
            f.write(text_out + "\n")


def main(
    db_path: str = None,
    repo: str = None,
    gh_proxy: str = None,
    game_version: GameVersionEnum = None,
    warcraft_base: str = None,
    export_region: RegionEnum = None,
    export_realms: Set[str] = None,
    cache: Cache = None,
    gh_api: GHAPI = None,
):
    if gh_api is None:
        if cache is None:
            cache_path = config.DEFAULT_CACHE_PATH
            cache = Cache(cache_path)

        gh_api = GHAPI(cache, gh_proxy=gh_proxy)

    if repo:
        mode = AuctionDB.MODE_REMOTE_R
    else:
        mode = AuctionDB.MODE_LOCAL_RW

    warcraft_path = os.path.join(warcraft_base, game_version.get_version_folder_name())
    export_path = TSMExporter.get_tsm_appdata_path(warcraft_path)
    db = AuctionDB(
        db_path,
        config.MARKET_VALUE_RECORD_EXPIRES,
        config.DEFAULT_DB_COMPRESS,
        mode,
        repo,
        gh_api,
    )

    namespace = Namespace(
        category=NameSpaceCategoriesEnum.DYNAMIC,
        game_version=game_version,
        region=export_region,
    )
    export_file = TextFile(export_path)
    exporter = TSMExporter(db, export_file)
    exporter.export_file.remove()
    exporter.export_region(namespace, export_realms)


def parse_args(raw_args):
    parser = argparse.ArgumentParser()
    default_db_path = config.DEFAULT_DB_PATH
    default_game_version = GameVersionEnum.RETAIL.name.lower()
    try:
        default_warcraft_base = TSMExporter.find_warcraft_base_windows()
    except Exception:
        default_warcraft_base = None

    parser.add_argument(
        "--db_path",
        type=str,
        default=default_db_path,
        help=f"path to the database, default: {default_db_path!r}",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Address of Github repo that's hosting the db files. If given, "
        "download and use repo's db instead of local ones. "
        "Note: local db will be overwritten.",
    )
    parser.add_argument(
        "--gh_proxy",
        type=str,
        default=None,
        help="URL of Github proxy server, for people having trouble accessing Github "
        "while using --repo option. "
        "Read more at https://github.com/crazypeace/gh-proxy, "
        "this program need a modified version that hosts API requests: "
        "https://github.com/hunshcn/gh-proxy/issues/44",
    )
    parser.add_argument(
        "--game_version",
        choices={e.name.lower() for e in GameVersionEnum},
        default=default_game_version,
        help=f"Game version to export, default: {default_game_version!r}",
    )
    parser.add_argument(
        "--warcraft_base",
        type=str,
        default=default_warcraft_base,
        help="Path to Warcraft installation directory, "
        "needed if the script is unable to locate it automatically, "
        f"default: {default_warcraft_base!r}",
    )
    parser.add_argument(
        "export_region",
        choices={e.value for e in RegionEnum},
        help="Region to export",
    )
    parser.add_argument(
        "export_realms",
        type=str,
        nargs="+",
        help="Realms to export, separated by space.",
    )
    args = parser.parse_args(raw_args)
    if not args.warcraft_base:
        raise ValueError(
            "Unable to locate Warcraft installation directory, "
            "please specify it with --warcraft_base option. "
            "Should be something like 'C:\\path_to\\World of Warcraft'."
        )
    args.game_version = GameVersionEnum[args.game_version.upper()]
    args.export_region = RegionEnum(args.export_region)
    args.export_realms = set(args.export_realms)

    return args


if __name__ == "__main__":
    logging.basicConfig(level=config.LOGGING_LEVEL)
    args = parse_args(sys.argv[1:])
    main(**vars(args))
