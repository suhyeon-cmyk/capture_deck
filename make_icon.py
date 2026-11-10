"""CaptureDeck 아이콘 생성 → capturedeck.ico (16~256px 포함)"""
from PIL import Image, ImageDraw, ImageFilter

S = 1024


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def make():
    # 배경: 남색 → 보라 대각선 그라데이션의 둥근 사각형
    grad = Image.new("RGB", (S, S))
    px = grad.load()
    c1, c2 = (24, 32, 72), (98, 46, 160)
    for y in range(S):
        for x in range(S):
            px[x, y] = lerp(c1, c2, (x + y) / (2 * S))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, S - 40, S - 40), radius=220, fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)

    # 위쪽 하이라이트
    hl = Image.new("L", (S, S), 0)
    hd = ImageDraw.Draw(hl)
    for y in range(S // 2):  # 위에서 아래로 서서히 사라지는 빛
        hd.line((0, y, S, y), fill=int(48 * (1 - y / (S / 2)) ** 1.5))
    img.paste((255, 255, 255), (0, 0), Image.composite(hl, Image.new("L", (S, S), 0), mask))

    # 모니터 화면 (흰 테두리) + 받침
    d.rounded_rectangle((190, 250, 834, 680), radius=60, outline=(255, 255, 255), width=56)
    d.rounded_rectangle((440, 690, 584, 770), radius=10, fill=(255, 255, 255))
    d.rounded_rectangle((340, 760, 684, 820), radius=30, fill=(255, 255, 255))

    # REC 빨간 점 + 은은한 빛
    cx, cy, r = 512, 465, 120
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((cx - r - 60, cy - r - 60, cx + r + 60, cy + r + 60), fill=(255, 70, 90, 140))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(40)))
    d = ImageDraw.Draw(img)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 64, 88))
    d.ellipse((cx - r + 30, cy - r + 25, cx - 10, cy - 20), fill=(255, 140, 155))  # 반사광
    return img


if __name__ == "__main__":
    img = make()
    img.save("capturedeck.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.resize((256, 256), Image.LANCZOS).save("capturedeck.png")
    print("ok")
