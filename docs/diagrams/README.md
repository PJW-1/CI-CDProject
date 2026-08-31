# 다이어그램 재생성

소스는 [`architecture.d2`](architecture.d2) 이며, D2로 SVG를 만든 뒤
헤드리스 Chrome으로 PNG를 뽑는다.

```bash
# 1) SVG
d2 --theme 300 --pad 50 architecture.d2 ../images/architecture.svg

# 2) PNG (2x 해상도)
#    d2 자체 PNG 출력은 Playwright 드라이버를 받아야 해서 오프라인/제한 환경에서 실패한다.
#    d2 의 마크다운 라벨은 SVG 안에서 foreignObject(HTML)로 렌더링되므로
#    순수 SVG 변환기(svglib 등)로는 글자가 나오지 않는다. 브라우저 렌더링이 필요하다.
chrome --headless --disable-gpu --hide-scrollbars \
       --force-device-scale-factor=2 --window-size=1000,1450 \
       --default-background-color=FFFFFFFF \
       --screenshot=../images/architecture.png \
       "file:///<절대경로>/docs/images/architecture.svg"
```

## 문서에서 PNG를 쓰는 이유

VS Code의 마크다운 미리보기는 보안 정책(CSP) 때문에 **SVG 이미지를 기본적으로 차단**한다.
GitHub에서는 SVG도 정상 표시되지만, 로컬 편집기에서 깨지지 않도록 PNG를 기본으로 참조한다.
SVG는 확대해도 깨지지 않으므로 인쇄·슬라이드용으로 함께 보관한다.
