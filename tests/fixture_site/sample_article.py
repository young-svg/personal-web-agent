"""Generate deterministic synthetic article and PNGs; no external images or model."""
from io import BytesIO
import json
from pathlib import Path
from PIL import Image, PngImagePlugin

ARTICLE = {'synthetic': True, 'title': '本地发布测试：合成文章',
           'body': '这是一篇仅供本地 fixture 测试的合成文章。\n第二段用于核验换行、图片预览与单次发布，不含真实个人资料。'}


def sample_image_bytes(color='steelblue', image_format='PNG'):
    image = Image.new('RGB', (96, 64), color)
    output = BytesIO()
    if image_format == 'PNG':
        info = PngImagePlugin.PngInfo()
        info.add_text('Title', 'FICTIONAL TEST IMAGE - NO PERSONAL DATA')
        image.save(output, format='PNG', pnginfo=info)
    else:
        image.save(output, format=image_format)
    return output.getvalue()


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[2] / '.local/publish-fixture'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = []
    for n, color in enumerate(['steelblue', 'darkorange'], 1):
        path = root / f'fictional-{n}.png'
        path.write_bytes(sample_image_bytes(color))
        paths.append(str(path.relative_to(root.parents[1])))
    (root/'body.txt').write_text(ARTICLE['body'], encoding='utf-8')
    (root/'article.json').write_text(json.dumps({**ARTICLE, 'images': paths}, ensure_ascii=False, indent=2))
    print(root/'article.json')
