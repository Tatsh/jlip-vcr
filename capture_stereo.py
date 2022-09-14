from os.path import splitext
from shlex import quote
from time import sleep
from typing import Any, cast
import asyncio
import asyncio.subprocess as asp
import os
import re
import subprocess as sp
import sys

from loguru import logger
from pytimeparse.timeparse import timeparse
import click
import psutil

from vcrtool import JLIPHRSeriesVCR, VTRMode

DEFAULT_TIMESPAN = '372m'
THREAD_QUEUE_SIZE = 2048


def _debug_sp_run(*args: Any, **kwargs: Any) -> sp.CompletedProcess[Any]:
    logger.debug(f'Executing: {" ".join(quote(x) for x in list(args[0]))}')
    return sp.run(*args, **kwargs)  # pylint: disable=subprocess-run-check


def _audio_device_is_available(audio_device: str) -> bool:
    logger.debug(f'Checking if {audio_device} can be used')
    return not ('Device or resource busy' in _debug_sp_run(  # pylint: disable=subprocess-run-check
        ('ffmpeg', '-hide_banner', '-f', 'alsa', '-i', audio_device),
        capture_output=True,
        text=True).stderr)


def _get_pipewire_audio_device_node_id(name: str) -> str | None:
    logger.debug(f'Getting node ID for "{name}"')
    try:
        if (m := re.search(r'(\d+)$', [
                item for item in sp.run(('wpctl', 'status'),
                                        text=True,
                                        capture_output=True,
                                        check=True).stdout.splitlines()
                if name in item
        ][0].split('.')[0])):
            logger.debug(f'Got node ID {m[0]}')
            return m[0]
    except IndexError:
        pass
    logger.debug('Failed to get node ID')
    return None


async def _a_debug_sleep(interval: int | float) -> None:
    logger.debug(
        f'Sleeping for {interval} {"seconds" if interval == 0 or interval > 1 else "second"}'
    )
    await asyncio.sleep(interval)


def _debug_sleep(interval: int | float) -> None:
    logger.debug(
        f'Sleeping for {interval} {"seconds" if interval == 0 or interval > 1 else "second"}'
    )
    sleep(interval)


async def _debug_create_subprocess_exec(*args: Any,
                                        **kwargs: Any) -> asp.Process:
    logger.debug(f'Executing: {" ".join(quote(x) for x in list(args))}')
    return await asp.create_subprocess_exec(*args, **kwargs)


async def _a_main(video_device: str, audio_device: str, length: int,
                  output: str, input_index: int, vbi_device: str | None,
                  vcr: JLIPHRSeriesVCR) -> int:
    logger.debug('Starting ffmpeg')
    length = int(length) + 15
    logger.debug(f'Will record for {length} seconds')
    output_base, _ = splitext(output)
    ffmpeg_proc = await _debug_create_subprocess_exec(
        'ffmpeg',
        '-hide_banner',
        '-loglevel',
        'warning',
        '-y',
        '-thread_queue_size',
        str(THREAD_QUEUE_SIZE),
        '-f',
        'v4l2',
        '-i',
        video_device,
        '-thread_queue_size',
        str(THREAD_QUEUE_SIZE),
        '-f',
        'alsa',
        '-i',
        audio_device,
        '-c:a',
        'flac',
        '-ac',
        '2',
        '-c:v',
        'libx265',
        '-x265-params',
        'lossless=1',
        '-preset',
        'superfast',
        '-flags',
        '+ilme+ildct',
        '-top',
        '1',
        '-aspect',
        '4/3',
        '-t',
        str(length),
        output,
        env=dict(FFREPORT=f'file={output_base}.log:level=40'),
        stdin=asp.PIPE)
    logger.debug(f'ffmpeg PID: {ffmpeg_proc.pid}')
    vbi_proc = None
    if vbi_device:
        output_vbi = f'{output_base}.vbi'
        try:
            os.remove(output_vbi)
        except FileNotFoundError:
            pass
        logger.debug(
            f'Starting zvbi2raw with device {vbi_device} and outputting to {output_vbi}'
        )
        vbi_proc = await _debug_create_subprocess_exec('zvbi2raw',
                                                       '-d',
                                                       vbi_device,
                                                       '-o',
                                                       f'{output_base}.vbi',
                                                       stdout=asp.PIPE,
                                                       stderr=asp.PIPE,
                                                       stdin=asp.PIPE)
        logger.debug(f'zvbi2raw PID: {vbi_proc.pid}')
    else:
        logger.debug('VBI device not specified')
    await _a_debug_sleep(2)
    logger.debug(f'Setting device {video_device} input to {input_index}')
    change_input_proc = await _debug_create_subprocess_exec('v4l2-ctl',
                                                            '-d',
                                                            video_device,
                                                            '-i',
                                                            str(input_index),
                                                            stdout=asp.PIPE,
                                                            stderr=asp.PIPE,
                                                            stdin=asp.PIPE)
    logger.debug(f'v4l2-ctl PID: {change_input_proc.pid}')
    await change_input_proc.wait()
    logger.debug(f'v4l2-ctl exited with code {change_input_proc.returncode}')
    if change_input_proc.returncode != 0:
        raise click.Abort('Failed to set input')
    await _a_debug_sleep(0.25)
    logger.debug('Resetting VCR counter')
    vcr.reset_counter()
    await _a_debug_sleep(1)
    logger.debug('Starting VCR playback')
    vcr.play()
    framerate = vcr.get_vtr_mode(fast=True).framerate
    ffmpeg_pid = ffmpeg_proc.pid
    try:
        while psutil.pid_exists(ffmpeg_pid):
            data = vcr.get_vtr_mode(fast=True)
            if data.vtr_mode != VTRMode.PLAY_FWD:
                logger.info(
                    f'Detected VCR is no longer playing (mode = {str(data.vtr_mode)}). '
                    'Terminating ffmpeg.')
                ffmpeg_proc.terminate()
                break
            print(
                f'{data.hour:02}:{data.minute:02}:{data.second:02}.'
                f'{(data.frame / framerate) * 1000:04.0f}',
                end='\r')
    except KeyboardInterrupt:
        logger.info('Received keyboard interrupt. Terminating ffmpeg.')
        ffmpeg_proc.terminate()
    # Waiting is required to avoid 'Loop that handles pid ... is closed'
    ffmpeg_proc_return = await ffmpeg_proc.wait()
    logger.debug(f'ffmpeg exited with code {ffmpeg_proc_return}')
    # ffmpeg always sets 255 if interrupted, but generally makes the file ready for use
    if ffmpeg_proc_return not in (0, 255):
        logger.warning('ffmpeg did not exit cleanly')
        return 1
    vbi_proc_return = None
    if vbi_proc:
        try:
            logger.debug('Terminating zvbi2raw')
            vbi_proc.terminate()
            vbi_proc_return = await vbi_proc.wait()
        except ProcessLookupError:
            pass
        logger.debug(
            f'zvbi2raw exited with code {vbi_proc_return or vbi_proc.returncode}'
        )
        if (vbi_proc_return or vbi_proc.returncode) != 0:
            logger.warning('vbi2raw did not complete successfully')
    return 0


@click.command()
@click.option('-s', '--serial', required=True)
@click.option('-a', '--audio-device', required=True)
@click.option('-n', '--audio-device-name', required=True)
@click.option('-v', '--video-device', required=True)
@click.option('-i', '--input-index', default=2, type=int)
@click.option('-b', '--vbi-device')
@click.option('-t', '--timespan', default='372m')
@click.argument('output')
def main(serial: str, audio_device: str, audio_device_name: str,
         video_device: str, vbi_device: str | None, timespan: str | None,
         output: str, input_index: int) -> int:
    timespan_seconds = timeparse(timespan or DEFAULT_TIMESPAN)
    if not timespan_seconds:
        click.secho('Timespan is invalid.', file=sys.stderr)
        raise click.Abort()
    audio_node_id = _get_pipewire_audio_device_node_id(audio_device_name)
    if not audio_node_id:
        click.secho('Unable to find audio node ID.', file=sys.stderr)
        raise click.Abort()
    logger.debug(f'Setting Pipewire device "{audio_device_name}" to Off')
    sp.run(('wpctl', 'set-profile', audio_node_id, '0'), check=True)
    _debug_sleep(0.1)
    if not _audio_device_is_available(audio_device):
        click.secho('Cannot use audio device.', file=sys.stderr)
        raise click.Abort()
    vcr = JLIPHRSeriesVCR(serial)
    logger.debug('Turning VCR on')
    vcr.turn_on()
    if not vcr.get_vtr_mode().tape_inserted:
        logger.error('No tape inserted')
        raise click.Abort()
    logger.debug('Rewinding tape')
    vcr.rewind_wait()
    logger.debug('Entering async')
    ret = asyncio.run(
        _a_main(video_device, audio_device, cast(int, timespan_seconds),
                output, input_index, vbi_device, vcr))
    logger.debug('Exiting async')
    logger.debug(f'Setting Pipewire device "{audio_device_name}" to On')
    sp.run(('wpctl', 'set-profile', audio_node_id, '1'), check=True)
    logger.debug('Rewinding tape')
    vcr.rewind_wait()
    sys.exit(ret)
