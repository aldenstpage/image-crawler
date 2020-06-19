import asyncio
import aiohttp
import wand.image
from functools import partial
from io import BytesIO
from PIL import Image, UnidentifiedImageError
from worker import settings as settings
from worker.stats_reporting import StatsManager
from wand.exceptions import WandException


async def process_image(
        persister, session, url, identifier, stats: StatsManager, source,
        semaphore, metadata_producer=None):
    """
    Get an image, collect dimensions metadata, thumbnail it, and persist it.
    :param stats: A StatsManager for recording task statuses.
    :param source: Used to determine rate limit policy. Example: flickr, behance
    :param semaphore: Limits concurrent execution of process_image tasks
    :param identifier: Our identifier for the image at the URL.
    :param persister: The function defining image persistence. It
    should do something like save an image to disk, or upload it to
    S3.
    :param session: An aiohttp client session.
    :param url: The URL of the image.
    :param metadata_producer: The outbound message queue for dimensions
    metadata.
    """
    async with semaphore:
        loop = asyncio.get_event_loop()
        try:
            img_resp = await session.get(url, source)
        except aiohttp.client_exceptions.ServerDisconnectedError:
            await stats.record_error(source, code="ServerDisconnected")
            return
        if not img_resp:
            await stats.record_error(source, code="NoRateToken")
            return
        elif img_resp.status >= 400:
            await stats.record_error(source, code=img_resp.status)
            return
        buffer = BytesIO(await img_resp.read())
        try:
            img = await loop.run_in_executor(None, partial(Image.open, buffer))
            if metadata_producer:
                notify_quality(img, buffer, identifier, metadata_producer)
                notify_exif(img, identifier, metadata_producer)
        except UnidentifiedImageError:
            await stats.record_error(
                source,
                code="UnidentifiedImageError"
            )
            return
        thumb = await loop.run_in_executor(
            None, partial(thumbnail_image, img)
        )
        await loop.run_in_executor(
            None, partial(persister, img=thumb, identifier=identifier)
        )
        await stats.record_success(source)


def thumbnail_image(img: Image):
    img.thumbnail(size=settings.TARGET_RESOLUTION, resample=Image.NEAREST)
    output = BytesIO()
    if img.mode != 'RGB':
        img = img.convert('RGB')
    img.save(output, format="JPEG", quality=30)
    output.seek(0)
    return output


def notify_quality(img: Image, buffer, identifier, metadata_producer):
    """ Collect quality metadata. """
    height, width = img.size
    filesize = buffer.getbuffer().nbytes
    buffer.seek(0)
    try:
        compression_quality = wand.image.Image(file=buffer).compression_quality
    except WandException:
        compression_quality = None
    metadata_producer.notify_image_quality_update(
        height, width, identifier, filesize, compression_quality
    )


def notify_exif(img: Image, identifier, metadata_producer):
    if 'exif' in img.info:
        exif = {hex(k): v for k, v in img.getexif().items()}
        if exif:
            metadata_producer.notify_exif_update(identifier, exif)


def save_thumbnail_s3(s3_client, img: BytesIO, identifier):
    s3_client.put_object(
        Bucket='cc-image-analysis',
        Key=f'{identifier}.jpg',
        Body=img
    )
