import argparse
from argparse import Namespace
from builtins import input
import configparser
import datetime
import logging
import math
import os
import pprint
import re
import time

import ee

import openet.ssebop as ssebop
print('running ssebop version: {}'.format(ssebop.__version__))
import openet.core
import openet.core.utils as utils

TOOL_NAME = 'tcorr_export_scene_by_wrs2'

# TODO: This could be a property or method of SSEBop or the Image class
TCORR_INDICES = {
    'GRIDDED': 0,
    'GRIDDED_COLD': 1,
    'GRIDDED_HOT': 2,
    'SCENE': 3,
    'MONTH': 4,
    'SEASON': 5,
    'ANNUAL': 6,
    'DEFAULT': 7,
    'USER': 8,
    'NODATA': 9,
}
EXPORT_GEO = [5000, 0, 15, 0, -5000, 15]


def main(ini_path=None, overwrite_flag=False, delay_time=0, gee_key_file=None,
         ready_task_max=-1, reverse_flag=False, tiles=None, update_flag=False,
         log_tasks=True, recent_days=0, start_dt=None, end_dt=None):
    """Compute gridded Tcorr images by WRS2 tile

    Parameters
    ----------
    ini_path : str
        Input file path.
    overwrite_flag : bool, optional
        If True, overwrite existing files if the export dates are the same and
        generate new images (but with different export dates) even if the tile
        lists are the same.  The default is False.
    delay_time : float, optional
        Delay time in seconds between starting export tasks (or checking the
        number of queued tasks, see "max_ready" parameter).  The default is 0.
    gee_key_file : str, None, optional
        Earth Engine service account JSON key file (the default is None).
    ready_task_max: int, optional
        Maximum number of queued "READY" tasks.
    reverse_flag : bool, optional
        If True, process WRS2 tiles in reverse order (the default is False).
    tiles : str, None, optional
        List of MGRS tiles to process (the default is None).
    update_flag : bool, optional
        If True, only overwrite scenes with an older model version.
        recent_days : int, optional
        Limit start/end date range to this many days before the current date
        (the default is 0 which is equivalent to not setting the parameter and
         will use the INI start/end date directly).
    start_dt : datetime, optional
        Override the start date in the INI file
        (the default is None which will use the INI start date).
    end_dt : datetime, optional
        Override the (inclusive) end date in the INI file
        (the default is None which will use the INI end date).

    """
    logging.info('\nCompute gridded Tcorr images by WRS2 tile')

    # CGM - Which format should we use for the WRS2 tile?
    wrs2_tile_fmt = 'p{:03d}r{:03d}'
    # wrs2_tile_fmt = '{:03d}{:03d}'
    wrs2_tile_re = re.compile('p?(\d{1,3})r?(\d{1,3})')

    # List of path/rows to skip
    wrs2_skip_list = [
        'p049r026',  # Vancouver Island, Canada
        # 'p047r031', # North California coast
        'p042r037',  # San Nicholas Island, California
        # 'p041r037', # South California coast
        'p040r038', 'p039r038', 'p038r038',  # Mexico (by California)
        'p037r039', 'p036r039', 'p035r039',  # Mexico (by Arizona)
        'p034r039', 'p033r039',  # Mexico (by New Mexico)
        'p032r040',  # Mexico (West Texas)
        'p029r041', 'p028r042', 'p027r043', 'p026r043',  # Mexico (South Texas)
        'p019r040', 'p018r040',  # West Florida coast
        'p016r043', 'p015r043',  # South Florida coast
        'p014r041', 'p014r042', 'p014r043',  # East Florida coast
        'p013r035', 'p013r036',  # North Carolina Outer Banks
        'p013r026', 'p012r026',  # Canada (by Maine)
        'p011r032',  # Rhode Island coast
    ]
    wrs2_path_skip_list = [9, 49]
    wrs2_row_skip_list = [25, 24, 43]

    mgrs_skip_list = []

    export_id_fmt = 'tcorr_gridded_{product}_{scene_id}'
    asset_id_fmt = '{coll_id}/{scene_id}'

    # TODO: Move to INI or function input parameter
    clip_ocean_flag = True

    # Read config file
    ini = configparser.ConfigParser(interpolation=None)
    ini.read_file(open(ini_path, 'r'))
    # ini = utils.read_ini(ini_path)

    model_name = 'SSEBOP'

    try:
        study_area_coll_id = str(ini['INPUTS']['study_area_coll'])
    except KeyError:
        raise ValueError('"study_area_coll" parameter was not set in INI')
    except Exception as e:
        raise e

    try:
        start_date = str(ini['INPUTS']['start_date'])
    except KeyError:
        raise ValueError('"start_date" parameter was not set in INI')
    except Exception as e:
        raise e

    try:
        end_date = str(ini['INPUTS']['end_date'])
    except KeyError:
        raise ValueError('"end_date" parameter was not set in INI')
    except Exception as e:
        raise e

    try:
        collections = str(ini['INPUTS']['collections'])
        collections = sorted([x.strip() for x in collections.split(',')])
    except KeyError:
        raise ValueError('"collections" parameter was not set in INI')
    except Exception as e:
        raise e

    try:
        mgrs_ftr_coll_id = str(ini['EXPORT']['mgrs_ftr_coll'])
    except KeyError:
        raise ValueError('"mgrs_ftr_coll" parameter was not set in INI')
    except Exception as e:
        raise e

    # Optional parameters
    try:
        study_area_property = str(ini['INPUTS']['study_area_property'])
    except KeyError:
        study_area_property = None
        logging.debug('  study_area_property: not set in INI, defaulting to None')
    except Exception as e:
        raise e

    try:
        study_area_features = str(ini['INPUTS']['study_area_features'])
        study_area_features = sorted([
            x.strip() for x in study_area_features.split(',')])
    except KeyError:
        study_area_features = []
        logging.debug('  study_area_features: not set in INI, defaulting to []')
    except Exception as e:
        raise e

    try:
        wrs2_tiles = str(ini['INPUTS']['wrs2_tiles']) \
            .replace('"', '').replace("'", '')
        wrs2_tiles = sorted([x.strip() for x in wrs2_tiles.split(',')])
    except KeyError:
        wrs2_tiles = []
        logging.debug('  wrs2_tiles: not set in INI, defaulting to []')
    except Exception as e:
        raise e

    try:
        mgrs_tiles = str(ini['EXPORT']['mgrs_tiles'])
        mgrs_tiles = sorted([x.strip() for x in mgrs_tiles.split(',')])
        # CGM - Remove empty strings caused by trailing or extra commas
        mgrs_tiles = [x.upper() for x in mgrs_tiles if x]
        logging.debug(f'  mgrs_tiles: {mgrs_tiles}')
    except KeyError:
        mgrs_tiles = []
        logging.debug('  mgrs_tiles: not set in INI, defaulting to []')
    except Exception as e:
        raise e

    try:
        utm_zones = str(ini['EXPORT']['utm_zones'])
        utm_zones = sorted([int(x.strip()) for x in utm_zones.split(',')])
        logging.debug(f'  utm_zones: {utm_zones}')
    except KeyError:
        utm_zones = []
        logging.debug('  utm_zones: not set in INI, defaulting to []')
    except Exception as e:
        raise e

    # TODO: Add try/except blocks and default values?
    cloud_cover = float(ini['INPUTS']['cloud_cover'])

    # Model specific parameters
    # Set the property name to lower case and try to cast values to numbers
    model_args = {
        k.lower(): float(v) if utils.is_number(v) else v
        for k, v in dict(ini[model_name]).items()}
    filter_args = {}

    # TODO: Add try/except blocks
    tmax_name = ini[model_name]['tmax_source']
    tcorr_source = ini[model_name]['tcorr_source']

    tcorr_scene_coll_id = '{}/{}_scene'.format(
        ini['EXPORT']['export_coll'], tmax_name.lower())

    if tcorr_source.upper() not in ['GRIDDED_COLD', 'GRIDDED']:
        raise ValueError('unsupported tcorr_source for these tools')

    # For now only support reading specific Tmax sources
    if tmax_name.upper() not in ['DAYMET_MEDIAN_V2']:
        raise ValueError(f'unsupported tmax_source: {tmax_name}')
    # if (tmax_name.upper() == 'CIMIS' and
    #         ini['INPUTS']['end_date'] < '2003-10-01'):
    #     raise ValueError('CIMIS is not currently available before 2003-10-01')
    # elif (tmax_name.upper() == 'DAYMET' and
    #         ini['INPUTS']['end_date'] > '2018-12-31'):
    #     logging.warning('\nDAYMET is not currently available past 2018-12-31, '
    #                     'using median Tmax values\n')

    # If the user set the tiles argument, use these instead of the INI values
    if tiles:
        logging.info('\nOverriding INI mgrs_tiles and utm_zones parameters')
        logging.info(f'  user tiles: {tiles}')
        mgrs_tiles = sorted([y.strip() for x in tiles for y in x.split(',')])
        mgrs_tiles = [x.upper() for x in mgrs_tiles if x]
        logging.info(f'  mgrs_tiles: {", ".join(mgrs_tiles)}')
        utm_zones = sorted(list(set([int(x[:2]) for x in mgrs_tiles])))
        logging.info(f'  utm_zones:  {", ".join(map(str, utm_zones))}')

    today_dt = datetime.datetime.now()
    today_dt = today_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if recent_days:
        logging.info('\nOverriding INI "start_date" and "end_date" parameters')
        logging.info(f'  Recent days: {recent_days}')
        end_dt = today_dt - datetime.timedelta(days=1)
        start_dt = today_dt - datetime.timedelta(days=recent_days)
        start_date = start_dt.strftime('%Y-%m-%d')
        end_date = end_dt.strftime('%Y-%m-%d')
    elif start_dt and end_dt:
        # Attempt to use the function start/end dates
        logging.info('\nOverriding INI "start_date" and "end_date" parameters')
        logging.info('  Custom date range')
        start_date = start_dt.strftime('%Y-%m-%d')
        end_date = end_dt.strftime('%Y-%m-%d')
    else:
        # Parse the INI start/end dates
        logging.info('\nINI date range')
        try:
            start_dt = datetime.datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.datetime.strptime(end_date, '%Y-%m-%d')
        except Exception as e:
            raise e
    logging.info(f'  Start: {start_date}')
    logging.info(f'  End:   {end_date}')

    # TODO: Add a few more checks on the dates
    if end_dt < start_dt:
        raise ValueError('end date can not be before start date')

    # logging.debug('\nInterpolation date range')
    # iter_start_dt = start_dt
    # iter_end_dt = end_dt + datetime.timedelta(days=1)
    # iter_start_dt = start_dt - datetime.timedelta(days=interp_days)
    # iter_end_dt = end_dt + datetime.timedelta(days=interp_days+1)
    # logging.debug('  Start: {}'.format(iter_start_dt.strftime('%Y-%m-%d')))
    # logging.debug('  End:   {}'.format(iter_end_dt.strftime('%Y-%m-%d')))

    logging.info('\nInitializing Earth Engine')
    if gee_key_file:
        logging.info(f'  Using service account key file: {gee_key_file}')
        # The "EE_ACCOUNT" parameter is not used if the key file is valid
        ee.Initialize(ee.ServiceAccountCredentials('x', key_file=gee_key_file))
    else:
        ee.Initialize()

    logging.debug('\nTmax properties')
    tmax_source = tmax_name.split('_', 1)[0]
    tmax_version = tmax_name.split('_', 1)[1]
    logging.debug(f'  Source:  {tmax_source}')
    logging.debug(f'  Version: {tmax_version}')
    # # DEADBEEF - Not needed with gridded Tcorr
    # # Get a Tmax image to set the Tcorr values to
    # if 'MEDIAN' in tmax_name.upper():
    #     tmax_coll_id = 'projects/earthengine-legacy/assets/' \
    #                    'projects/usgs-ssebop/tmax/{}'.format(tmax_name.lower())
    #     tmax_coll = ee.ImageCollection(tmax_coll_id)
    #     tmax_mask = ee.Image(tmax_coll.first()).select([0]).multiply(0)
    # # else:
    # #     raise ValueError(f'unsupported tmax_source: {tmax_name}')
    # logging.debug(f'  Collection: {tmax_coll_id}')

    # Build output collection and folder if necessary
    logging.debug(f'\nExport Collection: {tcorr_scene_coll_id}')
    if not ee.data.getInfo(tcorr_scene_coll_id.rsplit('/', 1)[0]):
        logging.info('\nExport folder does not exist and will be built'
                     '\n  {}'.format(tcorr_scene_coll_id.rsplit('/', 1)[0]))
        input('Press ENTER to continue')
        ee.data.createAsset({'type': 'FOLDER'}, tcorr_scene_coll_id.rsplit('/', 1)[0])
    if not ee.data.getInfo(tcorr_scene_coll_id):
        logging.info('\nExport collection does not exist and will be built'
                     '\n  {}'.format(tcorr_scene_coll_id))
        input('Press ENTER to continue')
        ee.data.createAsset({'type': 'IMAGE_COLLECTION'}, tcorr_scene_coll_id)

    # Get current running tasks
    tasks = utils.get_ee_tasks()
    ready_task_count = sum(1 for t in tasks.values() if t['state'] == 'READY')
    # ready_task_count = delay_task(ready_task_count, delay_time, max_ready)
    if logging.getLogger().getEffectiveLevel() == logging.DEBUG:
        utils.print_ee_tasks(tasks)
        input('ENTER')

    # DEADBEEF - The asset list will be retrieved before each WRS2 tile is processed
    # Get current asset list
    # logging.debug('\nGetting GEE asset list')
    # asset_list = utils.get_ee_assets(tcorr_scene_coll_id)
    # if logging.getLogger().getEffectiveLevel() == logging.DEBUG:
    #     pprint.pprint(asset_list[:10])

    # Get list of MGRS tiles that intersect the study area
    logging.debug('\nMGRS Tiles/Zones')
    export_list = mgrs_export_tiles(
        study_area_coll_id=study_area_coll_id,
        mgrs_coll_id=mgrs_ftr_coll_id,
        study_area_property=study_area_property,
        study_area_features=study_area_features,
        mgrs_tiles=mgrs_tiles,
        mgrs_skip_list=mgrs_skip_list,
        utm_zones=utm_zones,
        wrs2_tiles=wrs2_tiles,
    )
    if not export_list:
        logging.error('\nEmpty export list, exiting')
        return False
    # pprint.pprint(export_list)
    # input('ENTER')

    # Build the complete/filtered WRS2 list
    wrs2_tile_list = list(set(
        wrs2 for tile_info in export_list
        for wrs2 in tile_info['wrs2_tiles']))
    if wrs2_skip_list:
        wrs2_tile_list = [wrs2 for wrs2 in wrs2_tile_list
                          if wrs2 not in wrs2_skip_list]
    if wrs2_path_skip_list:
        wrs2_tile_list = [wrs2 for wrs2 in wrs2_tile_list
                          if int(wrs2[1:4]) not in wrs2_path_skip_list]
    if wrs2_row_skip_list:
        wrs2_tile_list = [wrs2 for wrs2 in wrs2_tile_list
                          if int(wrs2[5:8]) not in wrs2_row_skip_list]
    wrs2_tile_list = sorted(wrs2_tile_list, reverse=not (reverse_flag))
    wrs2_tile_count = len(wrs2_tile_list)

    # Process each WRS2 tile separately
    exists_ct, not_done_ct, submitted_ct, to_update_ct = 0, 0, 0, 0
    growing_season = 0
    logging.info('\nImage Exports')
    for wrs2_i, wrs2_tile in enumerate(wrs2_tile_list):
        wrs2_path, wrs2_row = map(int, wrs2_tile_re.findall(wrs2_tile)[0])
        logging.info('{} ({}/{})'.format(wrs2_tile, wrs2_i + 1, wrs2_tile_count))

        for coll_id in collections:
            filter_args[coll_id] = [
                {'type': 'equals', 'leftField': 'WRS_PATH', 'rightValue': wrs2_path},
                {'type': 'equals', 'leftField': 'WRS_ROW', 'rightValue': wrs2_row}]
        # logging.debug(f'  Filter Args: {filter_args}')

        # Build and merge the Landsat collections
        model_obj = ssebop.Collection(
            collections=collections,
            start_date=start_dt.strftime('%Y-%m-%d'),
            end_date=(end_dt + datetime.timedelta(days=1)).strftime('%Y-%m-%d'),
            cloud_cover_max=cloud_cover,
            geometry=ee.Geometry.Point(openet.core.wrs2.centroids[wrs2_tile]),
            model_args=model_args,
            filter_args=filter_args,
        )
        landsat_coll = model_obj.overpass(variables=['ndvi'])
        # pprint.pprint(landsat_coll.aggregate_array('system:id').getInfo())
        # input('ENTER')

        try:
            image_id_list = landsat_coll.aggregate_array('system:id').getInfo()
        except Exception as e:
            logging.warning('  Error getting image ID list, skipping tile')
            logging.debug(f'  {e}')
            continue

        # Get list of existing images for the target tile
        logging.debug('  Getting GEE asset list')
        asset_coll = ee.ImageCollection(tcorr_scene_coll_id) \
            .filterDate(start_dt.strftime('%Y-%m-%d'),
                        (end_dt + datetime.timedelta(days=1)).strftime('%Y-%m-%d')) \
            .filterMetadata('wrs2_tile', 'equals',
                            wrs2_tile_fmt.format(wrs2_path, wrs2_row))
        asset_props = {f'{tcorr_scene_coll_id}/{x["properties"]["system:index"]}':
                           x['properties']
                       for x in utils.get_info(asset_coll)['features']}
        # asset_props = {x['id']: x['properties'] for x in assets_info['features']}

        # Sort image ID list by date
        image_id_list = sorted(
            image_id_list, key=lambda k: k.split('/')[-1].split('_')[-1],
            reverse=reverse_flag)

        # Sort by date
        exists, not_done, submitted, to_update = [], [], [], []
        for image_id in image_id_list:
            coll_id, scene_id = image_id.rsplit('/', 1)
            # logging.info(f'{scene_id}')
            
            export_dt = datetime.datetime.strptime(scene_id.split('_')[-1], '%Y%m%d')
            valid_start = datetime.datetime(export_dt.year, 4, 1) - datetime.timedelta(days=48)
            valid_end = datetime.datetime(export_dt.year, 10, 31) + datetime.timedelta(days=48)
            if valid_start < export_dt < valid_end:
                growing_season += 1    
            
            export_date = export_dt.strftime('%Y-%m-%d')
            logging.debug(f'  Date: {export_date}')

            export_id = export_id_fmt.format(
                product=tmax_name.lower(), scene_id=scene_id)
            logging.debug(f'  Export ID: {export_id}')

            asset_id = asset_id_fmt.format(
                coll_id=tcorr_scene_coll_id, scene_id=scene_id)
            logging.debug(f'    Collection: {os.path.dirname(asset_id)}')
            logging.debug(f'    Image ID:   {os.path.basename(asset_id)}')

            if update_flag:
                def version_number(version_str):
                    return list(map(int, version_str.split('.')))

                if export_id in tasks.keys():
                    # logging.info('  Task already submitted, skipping')
                    submitted.append(asset_id)
                    continue
                # In update mode only overwrite if the version is old
                if asset_props and asset_id in asset_props.keys():
                    # model_ver = version_number(ssebop.__version__)
                    model_ver = version_number('0.2.0')
                    asset_ver = version_number(
                        asset_props[asset_id]['model_version'])

                    if asset_ver < model_ver:
                        to_update.append(asset_id)
                        continue
                        # logging.info('  Existing asset model version is old, '
                        #              'removing')
                        # logging.debug(f'    asset: {asset_ver}\n'
                        #               f'    model: {model_ver}')
                        # try:
                        #     ee.data.deleteAsset(asset_id)
                        # except:
                        #     logging.info('  Error removing asset, skipping')
                    elif (('T1_RT_TOA' in asset_props[asset_id]['coll_id']) and
                          ('T1_RT_TOA' not in image_id)):
                        continue
                        # logging.info('  Existing asset is from realtime '
                        #              'Landsat collection, removing')
                        # try:
                        #     ee.data.deleteAsset(asset_id)
                        # except:
                        #     logging.info('  Error removing asset, skipping')
                    else:
                        # logging.debug('  Asset is up to date, skipping')
                        exists.append(asset_id)
                        continue
            elif overwrite_flag:
                if export_id in tasks.keys():
                    # logging.info('  Task already submitted, cancelling')
                    ee.data.cancelTask(tasks[export_id]['id'])
                    submitted.append(asset_id)
                    continue

                # This is intentionally not an "elif" so that a task can be
                # cancelled and an existing image/file/asset can be removed
                # if asset_props and asset_id in asset_props.keys():
                #     logging.info('  Asset already exists, removing')
                #     ee.data.deleteAsset(asset_id)
            else:
                if export_id in tasks.keys():
                    # logging.debug('  Task already submitted, exiting')
                    submitted.append(asset_id)
                    continue
                elif asset_props and asset_id in asset_props.keys():
                    # logging.debug('  Asset already exists, skipping')
                    exists.append(asset_id)
                    continue

            not_done.append(asset_id)
            continue

        exists_ct += len(exists)
        not_done_ct += len(not_done)
        submitted_ct += len(submitted)
        to_update_ct += len(to_update)
        logging.info('          {} exist, {} not done, {} submitted, {} to update'.format(len(exists), len(not_done),
                                                                                          len(submitted), len(to_update)))
        logging.info('          {} growing season'.format(growing_season))

    logging.info('overall {} exist, {} not done, {} submitted, {} to update'.format(exists_ct, not_done_ct,
                                                                                      submitted_ct, to_update_ct))
# CGM - This is a modified copy of openet.utils.delay_task()
#   It was changed to take and return the number of ready tasks
#   This change may eventually be pushed to openet.utils.delay_task()
def delay_task(delay_time=0, task_max=-1, task_count=0):
    """Delay script execution based on number of READY tasks

    Parameters
    ----------
    delay_time : float, int
        Delay time in seconds between starting export tasks or checking the
        number of queued tasks if "ready_task_max" is > 0.  The default is 0.
        The delay time will be set to a minimum of 10 seconds if
        ready_task_max > 0.
    task_max : int, optional
        Maximum number of queued "READY" tasks.
    task_count : int
        The current/previous/assumed number of ready tasks.
        Value will only be updated if greater than or equal to ready_task_max.

    Returns
    -------
    int : ready_task_count

    """
    if task_max > 3000:
        raise ValueError('The maximum number of queued tasks must be less than 3000')

    # Force delay time to be a positive value since the parameter used to
    #   support negative values
    if delay_time < 0:
        delay_time = abs(delay_time)

    if ((task_max is None or task_max <= 0) and (delay_time >= 0)):
        # Assume task_max was not set and just wait the delay time
        logging.debug(f'  Pausing {delay_time} seconds, not checking task list')
        time.sleep(delay_time)
        return 0
    elif task_max and (task_count < task_max):
        # Skip waiting or checking tasks if a maximum number of tasks was set
        #   and the current task count is below the max
        logging.debug(f'  Ready tasks: {task_count}')
        return task_count

    # If checking tasks, force delay_time to be at least 10 seconds if
    #   ready_task_max is set to avoid excessive EE calls
    delay_time = max(delay_time, 10)

    # Make an initial pause before checking tasks lists to allow
    #   for previous export to start up
    # CGM - I'm not sure what a good default first pause time should be,
    #   but capping it at 30 seconds is probably fine for now
    logging.debug(f'  Pausing {min(delay_time, 30)} seconds for tasks to start')
    time.sleep(delay_time)

    # If checking tasks, don't continue to the next export until the number
    #   of READY tasks is greater than or equal to "ready_task_max"
    while True:
        ready_task_count = len(utils.get_ee_tasks(states=['READY']).keys())
        logging.debug(f'  Ready tasks: {ready_task_count}')
        if ready_task_count >= task_max:
            logging.debug(f'  Pausing {delay_time} seconds')
            time.sleep(delay_time)
        else:
            logging.debug(f'  {task_max - ready_task_count} open task '
                          f'slots, continuing processing')
            break

    return ready_task_count


def mgrs_export_tiles(study_area_coll_id, mgrs_coll_id,
                      study_area_property=None, study_area_features=[],
                      mgrs_tiles=[], mgrs_skip_list=[],
                      utm_zones=[], wrs2_tiles=[],
                      mgrs_property='mgrs', utm_property='utm',
                      wrs2_property='wrs2'):
    """Select MGRS tiles and metadata that intersect the study area geometry

    Parameters
    ----------
    study_area_coll_id : str
        Study area feature collection asset ID.
    mgrs_coll_id : str
        MGRS feature collection asset ID.
    study_area_property : str, optional
        Property name to use for inList() filter call of study area collection.
        Filter will only be applied if both 'study_area_property' and
        'study_area_features' parameters are both set.
    study_area_features : list, optional
        List of study area feature property values to filter on.
    mgrs_tiles : list, optional
        User defined MGRS tile subset.
    mgrs_skip_list : list, optional
        User defined list MGRS tiles to skip.
    utm_zones : list, optional
        User defined UTM zone subset.
    wrs2_tiles : list, optional
        User defined WRS2 tile subset.
    mgrs_property : str, optional
        MGRS property in the MGRS feature collection (the default is 'mgrs').
    utm_property : str, optional
        UTM zone property in the MGRS feature collection (the default is 'wrs2').
    wrs2_property : str, optional
        WRS2 property in the MGRS feature collection (the default is 'wrs2').

    Returns
    ------
    list of dicts: export information

    """
    # Build and filter the study area feature collection
    logging.debug('Building study area collection')
    logging.debug(f'  {study_area_coll_id}')
    study_area_coll = ee.FeatureCollection(study_area_coll_id)
    if (study_area_property == 'STUSPS' and
            'CONUS' in [x.upper() for x in study_area_features]):
        # Exclude AK, HI, AS, GU, PR, MP, VI, (but keep DC)
        study_area_features = [
            'AL', 'AR', 'AZ', 'CA', 'CO', 'CT', 'DC', 'DE', 'FL', 'GA',
            'IA', 'ID', 'IL', 'IN', 'KS', 'KY', 'LA', 'MA', 'MD', 'ME',
            'MI', 'MN', 'MO', 'MS', 'MT', 'NC', 'ND', 'NE', 'NH', 'NJ',
            'NM', 'NV', 'NY', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC', 'SD',
            'TN', 'TX', 'UT', 'VA', 'VT', 'WA', 'WI', 'WV', 'WY']
    # elif (study_area_property == 'STUSPS' and
    #         'WESTERN11' in [x.upper() for x in study_area_features]):
    #     study_area_features = [
    #         'AZ', 'CA', 'CO', 'ID', 'MT', 'NM', 'NV', 'OR', 'UT', 'WA', 'WY']
    study_area_features = sorted(list(set(study_area_features)))

    if study_area_property and study_area_features:
        logging.debug('  Filtering study area collection')
        logging.debug(f'  Property: {study_area_property}')
        logging.debug(f'  Features: {",".join(study_area_features)}')
        study_area_coll = study_area_coll.filter(
            ee.Filter.inList(study_area_property, study_area_features))

    logging.info('Building MGRS tile list')
    tiles_coll = ee.FeatureCollection(mgrs_coll_id) \
        .filterBounds(study_area_coll.geometry())

    # Filter collection by user defined lists
    if utm_zones:
        logging.debug(f'  Filter user UTM Zones:    {utm_zones}')
        tiles_coll = tiles_coll.filter(ee.Filter.inList(utm_property, utm_zones))
    if mgrs_skip_list:
        logging.debug(f'  Filter MGRS skip list:    {mgrs_skip_list}')
        tiles_coll = tiles_coll.filter(
            ee.Filter.inList(mgrs_property, mgrs_skip_list).Not())
    if mgrs_tiles:
        logging.debug(f'  Filter MGRS tiles/zones:  {mgrs_tiles}')
        # Allow MGRS tiles to be subsets of the full tile code
        #   i.e. mgrs_tiles = 10TE, 10TF
        mgrs_filters = [
            ee.Filter.stringStartsWith(mgrs_property, mgrs_id.upper())
            for mgrs_id in mgrs_tiles]
        tiles_coll = tiles_coll.filter(ee.call('Filter.or', mgrs_filters))

    def drop_geometry(ftr):
        return ee.Feature(None).copyProperties(ftr)

    logging.debug('  Requesting tile/zone info')
    tiles_info = utils.get_info(tiles_coll.map(drop_geometry))

    # Constructed as a list of dicts to mimic other interpolation/export tools
    tiles_list = []
    for tile_ftr in tiles_info['features']:
        tiles_list.append({
            'index': tile_ftr['properties']['mgrs'].upper(),
            'wrs2_tiles': sorted(utils.wrs2_str_2_set(
                tile_ftr['properties'][wrs2_property])),
        })

    # Apply the user defined WRS2 tile list
    if wrs2_tiles:
        logging.debug(f'  Filter WRS2 tiles: {wrs2_tiles}')
        for tile in tiles_list:
            tile['wrs2_tiles'] = sorted(list(
                set(tile['wrs2_tiles']) & set(wrs2_tiles)))

    # Only return export tiles that have intersecting WRS2 tiles
    export_list = [
        tile for tile in sorted(tiles_list, key=lambda k: k['index'])
        if tile['wrs2_tiles']]

    return export_list


def arg_parse():
    """"""
    parser = argparse.ArgumentParser(
        description='Compute/export gridded Tcorr images by WRS2 tile',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '-i', '--ini', type=utils.arg_valid_file,
        help='Input file', metavar='FILE')
    parser.add_argument(
        '--debug', default=logging.INFO, const=logging.DEBUG,
        help='Debug level logging', action='store_const', dest='loglevel')
    parser.add_argument(
        '--delay', default=0, type=float,
        help='Delay (in seconds) between each export tasks')
    parser.add_argument(
        '--key', type=utils.arg_valid_file, metavar='FILE',
        help='JSON key file')
    parser.add_argument(
        '--overwrite', default=False, action='store_true',
        help='Force overwrite of existing files')
    parser.add_argument(
        '--ready', default=-1, type=int,
        help='Maximum number of queued READY tasks')
    parser.add_argument(
        '--recent', default=0, type=int,
        help='Number of days to process before current date '
             '(ignore INI start_date and end_date')
    parser.add_argument(
        '--reverse', default=False, action='store_true',
        help='Process scenes/dates in reverse order')
    parser.add_argument(
        '--tiles', default='', nargs='+',
        help='Comma/space separated list of tiles to process')
    parser.add_argument(
        '--update', default=False, action='store_true',
        help='Update images with older model version numbers')
    parser.add_argument(
        '--start', type=utils.arg_valid_date, metavar='DATE', default=None,
        help='Start date (format YYYY-MM-DD)')
    parser.add_argument(
        '--end', type=utils.arg_valid_date, metavar='DATE', default=None,
        help='End date (format YYYY-MM-DD)')
    args = parser.parse_args()

    return args


if __name__ == "__main__":
    args = arg_parse()

    logging.basicConfig(level=args.loglevel, format='%(message)s')
    logging.getLogger('googleapiclient').setLevel(logging.ERROR)
    args = Namespace(delay=0, end=None,
                     ini='/home/dgketchum/PycharmProjects/openet-ssebop-beta/tcorr_gridded/ini/tcorr_processing.ini',
                     key=None, loglevel=20, overwrite=False, ready=1000, recent=0, reverse=False, start=None, tiles='',
                     update=True)
    main(ini_path=args.ini, overwrite_flag=args.overwrite,
         delay_time=args.delay, gee_key_file=args.key, ready_task_max=args.ready,
         reverse_flag=args.reverse, tiles=args.tiles, update_flag=args.update,
         recent_days=args.recent, start_dt=args.start, end_dt=args.end,
         )
