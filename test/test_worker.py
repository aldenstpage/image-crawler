import json
import pytest
import asyncio
import logging as log
import concurrent.futures
from worker.message import AsyncProducer
from test.mocks import (
    FakeConsumer, FakeAioSession, FakeRedis, AioNetworkSimulatingSession,
    FakeProducer
)
from worker.stats_reporting import StatsManager
from worker.image import process_image
from worker.rate_limit import RateLimitedClientSession
from PIL import Image


log.basicConfig(level=log.DEBUG)


def validate_thumbnail(img, identifier):
    """ Check that the image was resized. """
    i = Image.open(img)
    width, height = i.size
    assert width <= 640 and height <= 480


@pytest.mark.asyncio
async def test_pipeline():
    """ Test that the image processor completes with a fake image. """
    # validate_thumbnail callback performs the actual assertions
    redis = FakeRedis()
    stats = StatsManager(redis)
    await process_image(
        persister=validate_thumbnail,
        session=RateLimitedClientSession(FakeAioSession(), redis),
        url='https://example.gov/hello.jpg',
        identifier='4bbfe191-1cca-4b9e-aff0-1d3044ef3f2d',
        stats=stats,
        source='example',
        semaphore=asyncio.BoundedSemaphore(1000)
    )
    assert redis.store['num_resized'] == 1
    assert redis.store['num_resized:example'] == 1
    assert len(redis.store['status60s:example']) == 1


@pytest.mark.asyncio
async def test_handles_corrupt_images_gracefully():
    redis = FakeRedis()
    stats = StatsManager(redis)
    kafka = FakeProducer()
    producer = AsyncProducer(kafka)
    await process_image(
        persister=validate_thumbnail,
        session=RateLimitedClientSession(FakeAioSession(corrupt=True), redis),
        url='fake_url',
        identifier='4bbfe191-1cca-4b9e-aff0-1d3044ef3f2d',
        stats=stats,
        source='example',
        semaphore=asyncio.BoundedSemaphore(1000),
        metadata_producer=producer
    )
    producer_task = asyncio.create_task(producer.listen())
    try:
        await asyncio.wait_for(producer_task, 0.01)
    except concurrent.futures.TimeoutError:
        pass


@pytest.mark.asyncio
async def test_handled_404s():
    redis = FakeRedis()
    stats = StatsManager(redis)
    kafka = FakeProducer()
    rot_producer = AsyncProducer(kafka)
    session = RateLimitedClientSession(
        FakeAioSession(corrupt=True, status=404), redis
    )
    ident = '4bbfe191-1cca-4b9e-aff0-1d3044ef3f2d'
    await process_image(
        persister=validate_thumbnail,
        session=session,
        url='fake_url',
        identifier=ident,
        stats=stats,
        source='example',
        semaphore=asyncio.BoundedSemaphore(1000),
        rot_producer=rot_producer
    )
    producer_task = asyncio.create_task(rot_producer.listen())
    try:
        await asyncio.wait_for(producer_task, 0.01)
    except concurrent.futures.TimeoutError:
        pass
    rot_msg = kafka.messages[0]
    parsed = json.loads(str(rot_msg, 'utf-8'))
    assert ident == parsed['identifier']


@pytest.mark.asyncio
async def test_records_errors():
    redis = FakeRedis()
    stats = StatsManager(redis)
    session = RateLimitedClientSession(FakeAioSession(status=403), redis)
    retry_producer = FakeProducer()
    producer = AsyncProducer(retry_producer)
    await process_image(
        persister=validate_thumbnail,
        session=session,
        url='https://example.gov/image.jpg',
        identifier='4bbfe191-1cca-4b9e-aff0-1d3044ef3f2d',
        stats=stats,
        source='example',
        semaphore=asyncio.BoundedSemaphore(1000),
        retry_producer=producer
    )
    expected_keys = [
        'resize_errors',
        'resize_errors:example',
        'resize_errors:example:403',
        'status60s:example',
        'status1hr:example',
        'status12hr:example'
    ]
    for key in expected_keys:
        val = redis.store[key]
        assert val == 1 or len(val) == 1
    producer_task = asyncio.create_task(producer.listen())
    try:
        await asyncio.wait_for(producer_task, 0.01)
    except concurrent.futures.TimeoutError:
        pass
    retry = retry_producer.messages[0]
    parsed = json.loads(str(retry, 'utf-8'))
    assert parsed['attempts'] == 1


@pytest.fixture
@pytest.mark.asyncio
async def producer_fixture():
    # Run a processing task and capture the metadata results in a mock kafka
    # producer
    redis = FakeRedis()
    stats = StatsManager(redis)
    meta_producer = FakeProducer()
    retry_producer = FakeProducer()
    producer = AsyncProducer(meta_producer)
    await process_image(
        persister=validate_thumbnail,
        session=RateLimitedClientSession(FakeAioSession(), redis),
        url='https://example.gov/hello.jpg',
        identifier='4bbfe191-1cca-4b9e-aff0-1d3044ef3f2d',
        stats=stats,
        source='example',
        semaphore=asyncio.BoundedSemaphore(1000),
        metadata_producer=producer,
        retry_producer=retry_producer
    )
    producer_task = asyncio.create_task(producer.listen())
    try:
        await asyncio.wait_for(producer_task, 0.01)
    except concurrent.futures.TimeoutError:
        pass
    return meta_producer, retry_producer


def test_quality_messaging(producer_fixture):
    meta, _ = producer_fixture
    resolution_msg = meta.messages[0]
    parsed = json.loads(str(resolution_msg, 'utf-8'))
    expected_fields = [
        'height', 'width', 'identifier', 'compression_quality', 'filesize'
    ]
    for field in expected_fields:
        assert field in parsed
        assert parsed[field] is not None
        assert parsed[field] != ''


def test_exif_messaging(producer_fixture):
    meta, _ = producer_fixture
    exif_msg = meta.messages[1]
    parsed = json.loads(str(exif_msg, 'utf-8'))
    artist_key = '0x13b'
    assert parsed['exif'][artist_key] == 'unknown'


def test_retries(producer_fixture):
    _, retries = producer_fixture
    assert retries.messages == []
