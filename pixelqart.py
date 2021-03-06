import argparse
import base64
import io
import itertools
import os
import random
import tempfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from threading import Event
from typing import IO, Tuple
from urllib.parse import parse_qs, quote, urlparse

import requests
from PIL import Image
from pyzbar.pyzbar import decode as decode_barcode

QRCODE_SIZE = (41, 41)  # QR Code v6
QART_MARGIN = 4


def split_design(design: Image.Image) -> Tuple[Image.Image, Image.Image]:
    """Splits a design image to the desired part and necessary part. The design
    image may contain transparent pixels. Necessary In the design image,
    necessary black and white should be replaced with blue (#00f) and yellow
    (#ff0).
    """
    assert design.mode == 'RGBA'
    assert design.size == QRCODE_SIZE, design.size

    NECESSARY_BLACK = (0, 0, 255, 255)    # blue
    NECESSARY_WHITE = (255, 255, 0, 255)  # yellow

    desired = Image.new('RGBA', QRCODE_SIZE)
    necessary = Image.new('RGBA', QRCODE_SIZE)

    desired_pixels = desired.load()
    necessary_pixels = necessary.load()

    pixels = design.load()
    for x, y in itertools.product(range(design.width), range(design.height)):
        if pixels[x, y] == NECESSARY_BLACK:
            desired_pixels[x, y] = (0, 0, 0, 255)
            necessary_pixels[x, y] = (0, 0, 0, 255)
        elif pixels[x, y] == NECESSARY_WHITE:
            desired_pixels[x, y] = (255, 255, 255, 255)
            necessary_pixels[x, y] = (255, 255, 255, 255)
        else:
            desired_pixels[x, y] = pixels[x, y]

    return desired, necessary


def upload_image(filename: str) -> str:
    with open(filename, 'rb') as f:
        r = requests.post('https://research.swtch.com/qr/draw?upload=1',
                          files={'image': f}, allow_redirects=False)
    assert r.status_code == 302
    qs = parse_qs(urlparse(r.headers['Location']).query)
    return qs['i'][0]


def search_qrcode(name: str,
                  href: str,
                  uploaded_image_id: str,
                  necessary: Image.Image,
                  stop_event: Event,
                  stop_if_found: bool,
                  ) -> None:
    """Finds a QR Code including a pixel-art. A found QR Code must include the
    necessary part.
    """
    while not stop_event.is_set():
        mask = random.randrange(8)
        orient = random.randrange(4)
        seed = random.getrandbits(32)

        # https://github.com/rsc/swtch/blob/master/qrweb/play.go#L145
        url = ('https://research.swtch.com/qr/draw?x=0&y=0&c=0&'
               f'i={uploaded_image_id}&'
               'v=6&'  # QR Code version (v6 generates 41x41)
               'r=1&'  # Random Pixels
               'd=0&'  # Data Pixels Only
               't=0&'  # Dither (not implemented)
               'z=0&'  # Scale of source image
               f'u={quote(href, safe="")}&'
               f'm={mask}&'    # Mask pattern (0-7)
               f'o={orient}&'  # Rotation (0-3)
               f's={seed}'     # Random seed (int64)
               )
        print(f'Trying: {url}')

        # Generate a basic QR Code by QArt: https://research.swtch.com/qr/draw
        r = requests.get(url)
        _, data, *_ = r.content.decode().split('"')
        assert data.startswith('data:image/png;base64,')

        data = data[len('data:image/png;base64,'):]
        qrcode = Image.open(io.BytesIO(base64.b64decode(data)))

        # The essential size of the QR Code is 49x49 (41x41 + margin 4px) but
        # it is scaled up 4 times.
        size = (QRCODE_SIZE[0] + QART_MARGIN*2,
                QRCODE_SIZE[1] + QART_MARGIN*2)

        assert qrcode.width == 4 * size[0]
        assert qrcode.height == 4 * size[1]
        qrcode = qrcode.resize(size)

        # Paste the necessary part.
        canvas = Image.new('RGBA', size, (0, 0, 0, 0))
        canvas.paste(qrcode)
        canvas.paste(necessary, (QART_MARGIN, QART_MARGIN), mask=necessary)

        info = decode_barcode(canvas.resize((canvas.width*2, canvas.height*2)))
        ok = (len(info) == 1 and info[0].type == 'QRCODE')
        if not ok:
            continue

        # Found!
        print(f'Found: {url}')

        # Evaluation is CPU-intensive.
        with ProcessPoolExecutor(1) as ex:
            fut = ex.submit(eval_qrcode, canvas)
            score = fut.result()

        filename = f'{name}-{score}-m{mask}o{orient}s{seed}.png'
        canvas.save(filename)
        print(f'Saved: {filename} (score: {score}, url: {url})')

        if stop_if_found:
            stop_event.set()
            break


def eval_qrcode(qrcode: Image.Image) -> int:
    assert qrcode.width == QRCODE_SIZE[0] + QART_MARGIN*2
    assert qrcode.height == QRCODE_SIZE[1] + QART_MARGIN*2

    w, h = qrcode.size
    resized = qrcode.resize((w*10, h*10))
    expanded = Image.new('RGBA', (w*20, h*20), (0, 0, 0, 0))
    expanded.paste(resized, (w*5, h*5), mask=resized)
    qrcode = expanded

    success = 1
    for quality in range(1, 95+1):
        img = qrcode.convert('RGB')

        # Compress as JPEG
        buf = io.BytesIO()
        img.save(buf, format='jpeg', quality=quality)
        buf.seek(0)
        img = Image.open(buf)

        # Decode
        info = decode_barcode(img)
        ok = (len(info) == 1 and info[0].type == 'QRCODE')
        if ok:
            success += 1

    return success


parser = argparse.ArgumentParser()
parser.add_argument('design', type=argparse.FileType('rb'))
parser.add_argument('href')
parser.add_argument('-n', '--concurrency', type=int, default=16)
parser.add_argument('-x', '--stop-if-found', action='store_true')


def main(name: str,
         design_file: IO[bytes],
         href: str,
         concurrency: int = 16,
         stop_if_found: bool = False,
         ) -> None:
    with design_file, Image.open(design_file) as design:
        desired, necessary = split_design(design)

    with desired, tempfile.NamedTemporaryFile() as f:
        desired.save(f, format='PNG')
        uploaded_image_id = upload_image(f.name)

    stop_event = Event()
    ex = ThreadPoolExecutor()
    for i in range(concurrency):
        ex.submit(search_qrcode,
                  name, href, uploaded_image_id, necessary,
                  stop_event, stop_if_found)

    try:
        stop_event.wait()
    except KeyboardInterrupt:
        print('shutting down...')
    finally:
        stop_event.set()
        ex.shutdown()
        necessary.close()


if __name__ == '__main__':
    args = parser.parse_args()

    name, png = os.path.splitext(os.path.basename(args.design.name))
    assert png.lower() == '.png'

    main(name, args.design, args.href, args.concurrency, args.stop_if_found)
