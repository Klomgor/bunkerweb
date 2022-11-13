from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from logging import INFO, WARNING, Logger, getLogger
from os import _exit, getenv, listdir, path
from os.path import exists
from re import search
from sys import path as sys_path
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import scoped_session, sessionmaker
from time import sleep
from traceback import format_exc

from model import *

if "/usr/share/bunkerweb/utils" not in sys_path:
    sys_path.append("/usr/share/bunkerweb/utils")

from jobs import file_hash


class Database:
    def __init__(self, logger: Logger, sqlalchemy_string: str = None) -> None:
        """Initialize the database"""
        self.__logger = logger
        self.__sql_session = None
        self.__sql_engine = None

        getLogger("sqlalchemy.engine").setLevel(
            logger.level if logger.level != INFO else WARNING
        )

        if not sqlalchemy_string:
            sqlalchemy_string = getenv("DATABASE_URI", "sqlite:////data/db.sqlite3")

        if sqlalchemy_string.startswith("sqlite"):
            if not path.exists(sqlalchemy_string.split("///")[1]):
                open(sqlalchemy_string.split("///")[1], "w").close()

        self.__sql_engine = create_engine(
            sqlalchemy_string,
            encoding="utf-8",
            future=True,
            logging_name="sqlalchemy.engine",
        )
        not_connected = True
        retries = 5

        while not_connected:
            try:
                self.__sql_engine.connect()
                not_connected = False
            except SQLAlchemyError:
                if retries <= 0:
                    self.__logger.error(
                        f"Can't connect to database : {format_exc()}",
                    )
                    _exit(1)
                else:
                    self.__logger.warning(
                        "Can't connect to database, retrying in 5 seconds ...",
                    )
                    retries -= 1
                    sleep(5)

        self.__session = sessionmaker()
        self.__sql_session = scoped_session(self.__session)
        self.__sql_session.remove()
        self.__sql_session.configure(
            bind=self.__sql_engine, autoflush=False, expire_on_commit=False
        )

    def __del__(self) -> None:
        """Close the database"""
        if self.__sql_session:
            self.__sql_session.remove()

        if self.__sql_engine:
            self.__sql_engine.dispose()

    @contextmanager
    def __db_session(self):
        session = self.__sql_session()

        session.expire_on_commit = False

        try:
            yield session
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def set_autoconf_load(self, value: bool = True) -> str:
        """Set the autoconf_loaded value"""
        with self.__db_session() as session:
            try:
                metadata = session.query(Metadata).get(1)

                if metadata is None:
                    return "The metadata are not set yet, try again"

                metadata.autoconf_loaded = value
                session.commit()
            except BaseException:
                return format_exc()

        return ""

    def is_autoconf_loaded(self) -> bool:
        """Check if the autoconf is loaded"""
        with self.__db_session() as session:
            try:
                metadata = (
                    session.query(Metadata)
                    .with_entities(Metadata.autoconf_loaded)
                    .filter_by(id=1)
                    .first()
                )
                return metadata is not None and metadata.autoconf_loaded
            except (ProgrammingError, OperationalError):
                return False

    def is_first_config_saved(self) -> bool:
        """Check if the first configuration has been saved"""
        with self.__db_session() as session:
            try:
                metadata = (
                    session.query(Metadata)
                    .with_entities(Metadata.first_config_saved)
                    .filter_by(id=1)
                    .first()
                )
                return metadata is not None and metadata.first_config_saved
            except (ProgrammingError, OperationalError):
                return False

    def is_initialized(self) -> bool:
        """Check if the database is initialized"""
        with self.__db_session() as session:
            try:
                metadata = (
                    session.query(Metadata)
                    .with_entities(Metadata.is_initialized)
                    .filter_by(id=1)
                    .first()
                )
                return metadata is not None and metadata.is_initialized
            except (ProgrammingError, OperationalError):
                return False

    def initialize_db(self, version: str, integration: str = "Unknown") -> str:
        """Initialize the database"""
        with self.__db_session() as session:
            try:
                session.add(
                    Metadata(
                        is_initialized=True,
                        first_config_saved=False,
                        version=version,
                        integration=integration,
                    )
                )
                session.commit()
            except BaseException:
                return format_exc()

        return ""

    def init_tables(self, default_settings: List[Dict[str, str]]) -> Tuple[bool, str]:
        """Initialize the database tables and return the result"""
        inspector = inspect(self.__sql_engine)
        if len(Base.metadata.tables.keys()) <= len(inspector.get_table_names()):
            has_all_tables = True

            for table in Base.metadata.tables:
                if not inspector.has_table(table):
                    has_all_tables = False
                    break

            if has_all_tables:
                return False, ""

        Base.metadata.create_all(self.__sql_engine, checkfirst=True)

        to_put = []
        with self.__db_session() as session:
            for plugins in default_settings:
                if not isinstance(plugins, list):
                    plugins = [plugins]

                for plugin in plugins:
                    settings = {}
                    jobs = []
                    if "id" not in plugin:
                        settings = plugin
                        plugin = {
                            "id": "default",
                            "order": 999,
                            "name": "Default",
                            "description": "Default settings",
                            "version": "1.0.0",
                        }
                    else:
                        settings = plugin.pop("settings", {})
                        jobs = plugin.pop("jobs", [])

                    to_put.append(Plugins(**plugin))

                    for setting, value in settings.items():
                        value.update(
                            {
                                "plugin_id": plugin["id"],
                                "name": value["id"],
                                "id": setting,
                            }
                        )

                        for select in value.pop("select", []):
                            to_put.append(Selects(setting_id=value["id"], value=select))

                        to_put.append(
                            Settings(
                                **value,
                            )
                        )

                    for job in jobs:
                        to_put.append(Jobs(plugin_id=plugin["id"], **job))

                    if exists(f"/usr/share/bunkerweb/core/{plugin['id']}/ui"):
                        if {"template.html", "actions.py"}.issubset(
                            listdir(f"/usr/share/bunkerweb/core/{plugin['id']}/ui")
                        ):
                            with open(
                                f"/usr/share/bunkerweb/core/{plugin['id']}/ui/template.html",
                                "r",
                            ) as file:
                                template = file.read().encode("utf-8")
                            with open(
                                f"/usr/share/bunkerweb/core/{plugin['id']}/ui/actions.py",
                                "r",
                            ) as file:
                                actions = file.read().encode("utf-8")

                            to_put.append(
                                Plugin_pages(
                                    plugin_id=plugin["id"],
                                    template_file=template,
                                    template_checksum=sha256(template).hexdigest(),
                                    actions_file=actions,
                                    actions_checksum=sha256(actions).hexdigest(),
                                )
                            )

            try:
                session.add_all(to_put)
                session.commit()
            except BaseException:
                return False, format_exc()

        return True, ""

    def save_config(self, config: Dict[str, Any], method: str) -> str:
        """Save the config in the database"""
        to_put = []
        with self.__db_session() as session:
            # Delete all the old config
            session.query(Global_values).filter(Global_values.method == method).delete()
            session.query(Services_settings).filter(
                Services_settings.method == method
            ).delete()

            if config:
                if config["MULTISITE"] == "yes":
                    global_values = []
                    for server_name in config["SERVER_NAME"].split(" "):
                        if (
                            server_name
                            and session.query(Services)
                            .filter_by(id=server_name)
                            .first()
                            is None
                        ):
                            to_put.append(Services(id=server_name))

                        for key, value in deepcopy(config).items():
                            suffix = 0
                            if search(r"_\d+$", key):
                                suffix = int(key.split("_")[-1])
                                key = key[: -len(str(suffix)) - 1]

                            setting = (
                                session.query(Settings)
                                .with_entities(Settings.default)
                                .filter_by(id=key.replace(f"{server_name}_", ""))
                                .first()
                            )

                            if not setting:
                                continue

                            if server_name and key.startswith(server_name):
                                key = key.replace(f"{server_name}_", "")
                                service_setting = (
                                    session.query(Services_settings)
                                    .with_entities(Services_settings.value)
                                    .filter_by(
                                        service_id=server_name,
                                        setting_id=key,
                                        suffix=suffix,
                                    )
                                    .first()
                                )

                                if service_setting is None:
                                    if key != "SERVER_NAME" and (
                                        value == setting.default
                                        or (key in config and value == config[key])
                                    ):
                                        continue

                                    to_put.append(
                                        Services_settings(
                                            service_id=server_name,
                                            setting_id=key,
                                            value=value,
                                            suffix=suffix,
                                            method=method,
                                        )
                                    )
                                elif method == "autoconf":
                                    if key != "SERVER_NAME" and (
                                        value == setting.default
                                        or (key in config and value == config[key])
                                    ):
                                        session.query(Services_settings).filter(
                                            Services_settings.service_id == server_name,
                                            Services_settings.setting_id == key,
                                            Services_settings.suffix == suffix,
                                        ).delete()
                                    elif global_value.value != value:
                                        session.query(Services_settings).filter(
                                            Services_settings.service_id == server_name,
                                            Services_settings.setting_id == key,
                                            Services_settings.suffix == suffix,
                                        ).update(
                                            {
                                                Services_settings.value: value,
                                                Services_settings.method: method,
                                            }
                                        )
                            elif key not in global_values:
                                global_values.append(key)
                                global_value = (
                                    session.query(Global_values)
                                    .with_entities(Global_values.value)
                                    .filter_by(
                                        setting_id=key,
                                        suffix=suffix,
                                    )
                                    .first()
                                )

                                if global_value is None:
                                    if value == setting.default:
                                        continue

                                    to_put.append(
                                        Global_values(
                                            setting_id=key,
                                            value=value,
                                            suffix=suffix,
                                            method=method,
                                        )
                                    )
                                elif method == "autoconf":
                                    if value == setting.default:
                                        session.query(Global_values).filter(
                                            Global_values.setting_id == key,
                                            Global_values.suffix == suffix,
                                        ).delete()
                                    elif global_value.value != value:
                                        session.query(Global_values).filter(
                                            Global_values.setting_id == key,
                                            Global_values.suffix == suffix,
                                        ).update(
                                            {
                                                Global_values.value: value,
                                                Global_values.method: method,
                                            }
                                        )
                else:
                    primary_server_name = config["SERVER_NAME"].split(" ")[0]
                    to_put.append(Services(id=primary_server_name))

                    for key, value in config.items():
                        suffix = 0
                        if search(r"_\d+$", key):
                            suffix = int(key.split("_")[-1])
                            key = key[: -len(str(suffix)) - 1]

                        setting = (
                            session.query(Settings)
                            .with_entities(Settings.default)
                            .filter_by(id=key)
                            .first()
                        )

                        if setting and value == setting.default:
                            continue

                        global_value = (
                            session.query(Global_values)
                            .with_entities(Global_values.method)
                            .filter_by(setting_id=key, suffix=suffix)
                            .first()
                        )

                        if global_value is None:
                            to_put.append(
                                Global_values(
                                    setting_id=key,
                                    value=value,
                                    suffix=suffix,
                                    method=method,
                                )
                            )
                        elif global_value.method == method:
                            session.query(Global_values).filter(
                                Global_values.setting_id == key,
                                Global_values.suffix == suffix,
                            ).update({Global_values.value: value})

            try:
                metadata = session.query(Metadata).get(1)
                if metadata is not None and not metadata.first_config_saved:
                    metadata.first_config_saved = True
            except (ProgrammingError, OperationalError):
                pass

            try:
                session.add_all(to_put)
                session.commit()
            except BaseException:
                return format_exc()

        return ""

    def save_custom_configs(
        self, custom_configs: List[Dict[str, Tuple[str, List[str]]]], method: str
    ) -> str:
        """Save the custom configs in the database"""
        message = ""
        with self.__db_session() as session:
            # Delete all the old config
            session.query(Custom_configs).filter(
                Custom_configs.method == method
            ).delete()

            to_put = []
            endl = "\n"
            if custom_configs:
                for custom_config in custom_configs:
                    config = {
                        "data": custom_config["value"]
                        .replace("\\\n", "\n")
                        .encode("utf-8")
                        if isinstance(custom_config["value"], str)
                        else custom_config["value"].replace(b"\\\n", b"\n"),
                        "method": method,
                    }
                    config["checksum"] = sha256(config["data"]).hexdigest()

                    if custom_config["exploded"][0]:
                        if (
                            not session.query(Services)
                            .with_entities(Services.id)
                            .filter_by(id=custom_config["exploded"][0])
                            .first()
                        ):
                            message += f"{endl if message else ''}Service {custom_config['exploded'][0]} not found, please check your config"

                        config.update(
                            {
                                "service_id": custom_config["exploded"][0],
                                "type": custom_config["exploded"][1]
                                .replace("-", "_")
                                .lower(),
                                "name": custom_config["exploded"][2],
                            }
                        )
                    else:
                        config.update(
                            {
                                "type": custom_config["exploded"][1]
                                .replace("-", "_")
                                .lower(),
                                "name": custom_config["exploded"][2],
                            }
                        )

                    custom_conf = (
                        session.query(Custom_configs)
                        .with_entities(Custom_configs.checksum, Custom_configs.method)
                        .filter_by(
                            service_id=config.get("service_id", None),
                            type=config["type"],
                            name=config["name"],
                        )
                        .first()
                    )

                    if custom_conf is None:
                        to_put.append(Custom_configs(**config))
                    elif config["checksum"] != custom_conf.checksum and (
                        method == custom_conf.method or method == "autoconf"
                    ):
                        session.query(Custom_configs).filter(
                            Custom_configs.service_id == config.get("service_id", None),
                            Custom_configs.type == config["type"],
                            Custom_configs.name == config["name"],
                        ).update(
                            {
                                Custom_configs.data: config["data"],
                                Custom_configs.checksum: config["checksum"],
                            }
                            | (
                                {Custom_configs.method: "autoconf"}
                                if method == "autoconf"
                                else {}
                            )
                        )

            try:
                session.add_all(to_put)
                session.commit()
            except BaseException:
                return f"{f'{message}{endl}' if message else ''}{format_exc()}"

        return message

    def get_config(self, methods: bool = False) -> Dict[str, Any]:
        """Get the config from the database"""
        with self.__db_session() as session:
            config = {}
            for service in session.query(Services).with_entities(Services.id).all():
                for setting in (
                    session.query(Settings)
                    .with_entities(
                        Settings.id,
                        Settings.context,
                        Settings.default,
                        Settings.multiple,
                    )
                    .all()
                ):
                    suffix = 0
                    while True:
                        global_value = (
                            session.query(Global_values)
                            .with_entities(Global_values.value, Global_values.method)
                            .filter_by(setting_id=setting.id, suffix=suffix)
                            .first()
                        )

                        if global_value is None:
                            if suffix == 0:
                                config[setting.id] = (
                                    setting.default
                                    if methods is False
                                    else {"value": setting.default, "method": "default"}
                                )
                        else:
                            config[
                                setting.id + (f"_{suffix}" if suffix > 0 else "")
                            ] = (
                                global_value.value
                                if methods is False
                                else {
                                    "value": global_value.value,
                                    "method": global_value.method,
                                }
                            )

                        if setting.context != "multisite":
                            break

                        if suffix == 0:
                            config[f"{service.id}_{setting.id}"] = (
                                config[setting.id]
                                if methods is False
                                else {
                                    "value": config[setting.id]["value"],
                                    "method": "default",
                                }
                            )
                        elif f"{setting.id}_{suffix}" in config:
                            config[f"{service.id}_{setting.id}_{suffix}"] = (
                                config[f"{setting.id}_{suffix}"]
                                if methods is False
                                else {
                                    "value": config[f"{setting.id}_{suffix}"]["value"],
                                    "method": "default",
                                }
                            )

                        service_setting = (
                            session.query(Services_settings)
                            .with_entities(
                                Services_settings.value, Services_settings.method
                            )
                            .filter_by(
                                service_id=service.id,
                                setting_id=setting.id,
                                suffix=suffix,
                            )
                            .first()
                        )

                        if service_setting is not None:
                            config[
                                f"{service.id}_{setting.id}"
                                + (f"_{suffix}" if suffix > 0 else "")
                            ] = (
                                service_setting.value
                                if methods is False
                                else {
                                    "value": service_setting.value,
                                    "method": service_setting.method,
                                }
                            )
                        elif suffix > 0:
                            break

                        if not setting.multiple:
                            break

                        suffix += 1

            return config

    def get_custom_configs(self) -> List[Dict[str, Any]]:
        """Get the custom configs from the database"""
        with self.__db_session() as session:
            return [
                {
                    "service_id": custom_config.service_id,
                    "type": custom_config.type,
                    "name": custom_config.name,
                    "data": custom_config.data,
                    "method": custom_config.method,
                }
                for custom_config in (
                    session.query(Custom_configs)
                    .with_entities(
                        Custom_configs.service_id,
                        Custom_configs.type,
                        Custom_configs.name,
                        Custom_configs.data,
                        Custom_configs.method,
                    )
                    .all()
                )
            ]

    def get_services_settings(self, methods: bool = False) -> List[Dict[str, Any]]:
        """Get the services' configs from the database"""
        services = []
        config = self.get_config(methods=methods)
        with self.__db_session() as session:
            for service in session.query(Services).with_entities(Services.id).all():
                tmp_config = deepcopy(config)

                for key, value in deepcopy(tmp_config).items():
                    if key.startswith(f"{service.id}_"):
                        tmp_config[key.replace(f"{service.id}_", "")] = value

                services.append(tmp_config)

        return services

    def update_job(self, plugin_id: str, job_name: str, success: bool) -> str:
        """Update the job last_run in the database"""
        with self.__db_session() as session:
            job = (
                session.query(Jobs)
                .filter_by(plugin_id=plugin_id, name=job_name)
                .first()
            )

            if job is None:
                return "Job not found"

            job.last_run = datetime.now()
            job.success = success

            try:
                session.commit()
            except BaseException:
                return format_exc()

        return ""

    def update_job_cache(
        self,
        job_name: str,
        service_id: Optional[str],
        file_name: str,
        data: bytes,
        *,
        checksum: str = None,
    ) -> str:
        """Update the plugin cache in the database"""
        with self.__db_session() as session:
            cache = (
                session.query(Job_cache)
                .filter_by(
                    job_name=job_name, service_id=service_id, file_name=file_name
                )
                .first()
            )

            if cache is None:
                session.add(
                    Job_cache(
                        job_name=job_name,
                        service_id=service_id,
                        file_name=file_name,
                        data=data,
                        last_update=datetime.now(),
                        checksum=checksum,
                    )
                )
            else:
                cache.data = data
                cache.last_update = datetime.now()
                cache.checksum = checksum

            try:
                session.commit()
            except BaseException:
                return format_exc()

        return ""

    def update_external_plugins(self, plugins: List[Dict[str, Any]]) -> str:
        """Update external plugins from the database"""
        to_put = []
        with self.__db_session() as session:
            db_plugins = (
                session.query(Plugins)
                .with_entities(Plugins.id)
                .filter_by(external=True)
                .all()
            )

            db_ids = []
            if db_plugins is not None:
                ids = [plugin["id"] for plugin in plugins]
                missing_ids = [
                    plugin.id for plugin in db_plugins if plugin.id not in ids
                ]

                # Remove plugins that are no longer in the list
                session.query(Plugins).filter(Plugins.id.in_(missing_ids)).delete()

            for plugin in plugins:
                settings = plugin.pop("settings", {})
                jobs = plugin.pop("jobs", [])
                pages = plugin.pop("pages", [])
                plugin["external"] = True

                if plugin["id"] in db_ids:
                    db_plugin = session.query(Plugins).get(plugin["id"])

                    if db_plugin is not None:
                        if db_plugin.external is False:
                            self.__logger.warning(
                                f"Plugin {plugin['id']} is not external, skipping update (updating a non-external plugin is forbidden for security reasons)"
                            )
                            continue

                        updates = {}

                        if plugin["order"] != db_plugin.order:
                            updates[Plugins.order] = plugin["order"]

                        if plugin["name"] != db_plugin.name:
                            updates[Plugins.name] = plugin["name"]

                        if plugin["description"] != db_plugin.description:
                            updates[Plugins.description] = plugin["description"]

                        if plugin["version"] != db_plugin.version:
                            updates[Plugins.version] = plugin["version"]

                        if updates:
                            session.query(Plugins).filter(
                                Plugins.id == plugin["id"]
                            ).update(updates)

                        db_settings = (
                            session.query(Settings)
                            .filter_by(plugin_id=plugin["id"])
                            .all()
                        )
                        setting_ids = [setting["id"] for setting in settings.values()]
                        missing_ids = [
                            setting.id
                            for setting in db_settings
                            if setting.id not in setting_ids
                        ]

                        # Remove settings that are no longer in the list
                        session.query(Settings).filter(
                            Settings.id.in_(missing_ids)
                        ).delete()

                        for setting, value in settings.items():
                            value.update(
                                {
                                    "plugin_id": plugin["id"],
                                    "name": value["id"],
                                    "id": setting,
                                }
                            )
                            db_setting = session.query(Settings).get(setting)

                            if setting not in db_ids or db_setting is None:
                                for select in value.pop("select", []):
                                    to_put.append(
                                        Selects(setting_id=value["id"], value=select)
                                    )

                                to_put.append(
                                    Settings(
                                        **value,
                                    )
                                )
                            else:
                                updates = {}

                                if value["name"] != db_setting.name:
                                    updates[Settings.name] = value["name"]

                                if value["context"] != db_setting.context:
                                    updates[Settings.context] = value["context"]

                                if value["default"] != db_setting.default:
                                    updates[Settings.default] = value["default"]

                                if value["help"] != db_setting.help:
                                    updates[Settings.help] = value["help"]

                                if value["label"] != db_setting.label:
                                    updates[Settings.label] = value["label"]

                                if value["regex"] != db_setting.regex:
                                    updates[Settings.regex] = value["regex"]

                                if value["type"] != db_setting.type:
                                    updates[Settings.type] = value["type"]

                                if value["multiple"] != db_setting.multiple:
                                    updates[Settings.multiple] = value["multiple"]

                                if updates:
                                    session.query(Settings).filter_by(
                                        Settings.id == setting
                                    ).update(updates)

                                db_selects = (
                                    session.query(Selects)
                                    .filter_by(setting_id=setting)
                                    .all()
                                )
                                select_values = [
                                    select["value"]
                                    for select in value.get("select", [])
                                ]
                                missing_values = [
                                    select.value
                                    for select in db_selects
                                    if select.value not in select_values
                                ]

                                # Remove selects that are no longer in the list
                                session.query(Selects).filter(
                                    Selects.value.in_(missing_values)
                                ).delete()

                                for select in value.get("select", []):
                                    db_select = session.query(Selects).get(
                                        (setting, select)
                                    )

                                    if db_select is None:
                                        to_put.append(
                                            Selects(setting_id=setting, value=select)
                                        )

                        db_jobs = (
                            session.query(Jobs).filter_by(plugin_id=plugin["id"]).all()
                        )
                        job_names = [job["name"] for job in jobs]
                        missing_names = [
                            job.name for job in db_jobs if job.name not in job_names
                        ]

                        # Remove jobs that are no longer in the list
                        session.query(Jobs).filter(
                            Jobs.name.in_(missing_names)
                        ).delete()

                        for job in jobs:
                            db_job = session.query(Jobs).get(job["name"])

                            if job["name"] not in db_ids or db_job is None:
                                to_put.append(
                                    Jobs(
                                        plugin_id=plugin["id"],
                                        **job,
                                    )
                                )
                            else:
                                updates = {}

                                if job["file"] != db_job.file:
                                    updates[Jobs.file] = job["file"]

                                if job["every"] != db_job.every:
                                    updates[Jobs.every] = job["every"]

                                if job["reload"] != db_job.reload:
                                    updates[Jobs.reload] = job["reload"]

                                if updates:
                                    updates[Jobs.last_update] = None
                                    session.query(Job_cache).filter_by(
                                        job_name=job["name"]
                                    ).delete()
                                    session.query(Jobs).filter_by(
                                        Jobs.name == job["name"]
                                    ).update(updates)

                        if exists(f"/usr/share/bunkerweb/core/{plugin['id']}/ui"):
                            if {"template.html", "actions.py"}.issubset(
                                listdir(f"/usr/share/bunkerweb/core/{plugin['id']}/ui")
                            ):
                                db_plugin_page = (
                                    session.query(Plugin_pages)
                                    .filter_by(plugin_id=plugin["id"])
                                    .first()
                                )

                                if db_plugin_page is None:
                                    with open(
                                        f"/usr/share/bunkerweb/core/{plugin['id']}/ui/template.html",
                                        "r",
                                    ) as file:
                                        template = file.read().encode("utf-8")
                                    with open(
                                        f"/usr/share/bunkerweb/core/{plugin['id']}/ui/actions.py",
                                        "r",
                                    ) as file:
                                        actions = file.read().encode("utf-8")

                                    to_put.append(
                                        Plugin_pages(
                                            plugin_id=plugin["id"],
                                            template_file=template,
                                            template_checksum=sha256(
                                                template
                                            ).hexdigest(),
                                            actions_file=actions,
                                            actions_checksum=sha256(
                                                actions
                                            ).hexdigest(),
                                        )
                                    )
                                else:
                                    updates = {}
                                    template_checksum = file_hash(
                                        f"/usr/share/bunkerweb/core/{plugin['id']}/ui/template.html"
                                    )
                                    actions_checksum = file_hash(
                                        f"/usr/share/bunkerweb/core/{plugin['id']}/ui/actions.py"
                                    )

                                    if (
                                        template_checksum
                                        != db_plugin_page.template_checksum
                                    ):
                                        with open(
                                            f"/usr/share/bunkerweb/core/{plugin['id']}/ui/template.html",
                                            "r",
                                        ) as file:
                                            updates.update(
                                                {
                                                    Plugin_pages.template_file: file.read().encode(
                                                        "utf-8"
                                                    ),
                                                    Plugin_pages.template_checksum: template_checksum,
                                                }
                                            )

                                    if (
                                        actions_checksum
                                        != db_plugin_page.actions_checksum
                                    ):
                                        with open(
                                            f"/usr/share/bunkerweb/core/{plugin['id']}/ui/actions.py",
                                            "r",
                                        ) as file:
                                            updates.update(
                                                {
                                                    Plugin_pages.actions_file: file.read().encode(
                                                        "utf-8"
                                                    ),
                                                    Plugin_pages.actions_checksum: actions_checksum,
                                                }
                                            )

                                    if updates:
                                        session.query(Plugin_pages).filter(
                                            Plugin_pages.plugin_id == plugin["id"]
                                        ).update(updates)

                        continue

                to_put.append(Plugins(**plugin))

                for setting, value in settings.items():
                    value.update(
                        {
                            "plugin_id": plugin["id"],
                            "name": value["id"],
                            "id": setting,
                        }
                    )

                    for select in value.pop("select", []):
                        to_put.append(Selects(setting_id=value["id"], value=select))

                    to_put.append(
                        Settings(
                            **value,
                        )
                    )

                for job in jobs:
                    to_put.append(Jobs(plugin_id=plugin["id"], **job))

                for page in pages:
                    to_put.append(
                        Plugin_pages(
                            plugin_id=plugin["id"],
                            template_file=page["template_file"],
                            template_checksum=sha256(page["template_file"]).hexdigest(),
                            actions_file=page["actions_file"],
                            actions_checksum=sha256(page["actions_file"]).hexdigest(),
                        )
                    )

            try:
                session.add_all(to_put)
                session.commit()
            except BaseException:
                return format_exc()

        return ""