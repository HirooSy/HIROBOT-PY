import inspect
import os
import re

from lib.utils import helper
from lib.utils.database import MongoDB, MySqlAdapter, SQLiteAdapter
from lib.utils.handler import PluginError


def local_db_path():
    prefix_args = helper.opts.get('_') or []
    prefix = prefix_args[0] + '_' if prefix_args else ''
    return os.path.join(os.getcwd(), 'data', f'{prefix}database.db')


def detect_cloud_type(url):
    if not url:
        return None
    if re.match(r'^mongodb(\+srv)?://', url, re.I):
        return 'mongodb'
    if re.match(r'^mysql://', url, re.I):
        return 'mysql'
    return None


def get_cloud_adapter(url, cloud_type):
    if cloud_type == 'mongodb':
        return MongoDB(url, {})
    if cloud_type == 'mysql':
        return MySqlAdapter(url)
    raise RuntimeError('Unknown cloud database type')


def is_plain_obj(v):
    return isinstance(v, dict)


def merge_data(target, source):
    target = target if isinstance(target, dict) else {}
    source = source if isinstance(source, dict) else {}
    for key, src_val in source.items():
        if is_plain_obj(src_val):
            if not is_plain_obj(target.get(key)):
                target[key] = {}
            target[key] = {**target[key], **src_val}
        else:
            target[key] = src_val
    return target


def count_entries(data):
    if not isinstance(data, dict):
        return 0
    return sum(len(v) if is_plain_obj(v) else 1 for v in data.values())


async def close_adapter(adapter):
    try:
        closer = getattr(adapter, 'close', None)
        if closer:
            result = closer()
            if inspect.isawaitable(result):
                await result
    except Exception:
        return None


async def handler(m, *, usedPrefix, command, text):
    p, command, text = usedPrefix, command, text
    direction = (text or '').strip().lower()
    if direction not in ('local-cloud', 'cloud-local'):
        raise PluginError(f'Invalid format!\n\n*{p + command} local-cloud*\n> Migrate data from the local file (data/database.db) to the cloud database (Mongo/MySQL, depending on the DATABASE env)\n\n*{p + command} cloud-local*\n> Migrate data from the cloud database (Mongo/MySQL) to the local file (data/database.db)\n\n_This command auto-detects MongoDB or MySQL from the DATABASE env value._')
    db_url = os.environ.get('DATABASE', '')
    cloud_type = detect_cloud_type(db_url)
    local_path = local_db_path()
    local_exists = os.path.exists(local_path)
    if direction == 'local-cloud':
        if not local_exists:
            raise PluginError('❌ File *data/database.db* not found.\n\nThere is no local data to migrate.')
        if not cloud_type:
            raise PluginError('❌ The *DATABASE* env is not set to a valid MongoDB/MySQL token.\n\nSet the *DATABASE* env to a Mongo (mongodb://...) or MySQL (mysql://...) connection string before migrating to the cloud.')
        await m.reply(f'Starting migration *local ➜ {cloud_type.upper()}*, please wait...')
        local_adapter = SQLiteAdapter(local_path)
        try:
            local_data = await local_adapter.read()
        finally:
            await close_adapter(local_adapter)
        if not local_data or count_entries(local_data) == 0:
            raise PluginError('❌ Data in *data/database.db* is empty, nothing to migrate.')
        cloud_adapter = get_cloud_adapter(db_url, cloud_type)
        try:
            cloud_data = (await cloud_adapter.read()) or {}
        except Exception:
            cloud_data = {}
        before_count = count_entries(cloud_data)
        merged = merge_data(cloud_data, local_data)
        await cloud_adapter.write(merged)
        await close_adapter(cloud_adapter)
        return await m.reply(f'✅ Migration *local ➜ {cloud_type.upper()}* completed!\n\n- Local data: {count_entries(local_data)} entries\n- Cloud data before: {before_count} entries\n- Cloud data now: {count_entries(merged)} entries\n\n_Only copies data, nothing was deleted from either local or cloud._')
    if not cloud_type:
        raise PluginError('❌ The *DATABASE* env is not set to a valid MongoDB/MySQL token.')
    if local_exists:
        raise PluginError('❌ File *data/database.db* already exists.\n\nThis command only runs when there is no local database yet, to avoid an unwanted migration. Remove/move *data/database.db* first if you really want to pull the data from the cloud again.')
    await m.reply(f'Starting migration *{cloud_type.upper()} ➜ local*, please wait...')
    cloud_adapter = get_cloud_adapter(db_url, cloud_type)
    try:
        cloud_data = await cloud_adapter.read()
    finally:
        await close_adapter(cloud_adapter)
    if not cloud_data or count_entries(cloud_data) == 0:
        raise PluginError(f'❌ Data in the cloud database ({cloud_type.upper()}) is empty, nothing to migrate.')
    os.makedirs(os.path.join(os.getcwd(), 'data'), exist_ok=True)
    local_adapter = SQLiteAdapter(local_path)
    try:
        existing_local = (await local_adapter.read()) or {}
        merged = merge_data(existing_local, cloud_data)
        await local_adapter.write(merged)
    finally:
        await close_adapter(local_adapter)
    return await m.reply(f'✅ Migration *{cloud_type.upper()} ➜ local* completed!\n\n- Cloud data: {count_entries(cloud_data)} entries\n- Local data now: {count_entries(merged)} entries\n- Saved to: data/database.db\n\n_Only copies data, nothing was deleted from either cloud or local._')


command = re.compile(r'^migratedb$', re.I)
tags = ['owner']
help = ['migratedb <local-cloud/cloud-local>']
rowner = True
ai = {'risk': 'low', 'description': 'migrate database contents between local sqlite and mongodb/mysq'}
