#!/bin/bash
# 打一个别人能双击的 macOS 安装包。
#
# 包里自带 Python 3.12 与全部依赖：装的人不需要仓库、不需要 uv、不需要网络。
# 装完只做「系统那一半」——建 _amazonads、铺代码、写配置模板、建导出目录、
# 装 LaunchDaemon、链 /usr/local/bin/amazon-ads。服务不启动（还没填密钥），
# 孩子那一半也不做（装包时还不知道孩子是谁）。
#
# 用法：bash packaging/build-pkg.sh          # 不签名，自己机器上验
#       bash packaging/build-pkg.sh --sign "Developer ID Installer: 某某 (TEAMID)"
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="/Library/Application Support/amazon-ads"     # 目标绝对路径；venv 必须照它建
IDENT="io.github.helloyoung2025.amazon-ads"
BUILD="$REPO/build/pkg"
STAGE="$BUILD/root"
SCRIPTS="$BUILD/scripts"
SIGN=""
[ "${1:-}" = "--sign" ] && SIGN="${2:?--sign 后面要跟证书名}"

VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO/pyproject.toml" | head -1)"
ARCH="$(uname -m)"
OUT="$REPO/dist/amazon-ads-$VERSION-$ARCH.pkg"

echo "== 1/6 清理 =="
rm -rf "$BUILD"; mkdir -p "$STAGE$ROOT" "$SCRIPTS" "$REPO/dist"

echo "== 2/6 打 wheel =="
cd "$REPO" && uv build --wheel >/dev/null
WHEEL="$(ls -t "$REPO"/dist/ads_control_plane-*.whl | head -1)"

echo "== 3/6 装一份独立 Python $( sed -n 's/^PYTHON_VERSION = "\(.*\)"/\1/p' "$REPO/src/ads_control_plane/sfw/installer.py" | head -1 ) =="
PYVER="$(sed -n 's/^PYTHON_VERSION = "\(.*\)"/\1/p' "$REPO/src/ads_control_plane/sfw/installer.py" | head -1)"
ARCH="$(uname -m | sed 's/^arm64$/aarch64/')"
mkdir -p "$STAGE$ROOT/python"
# 优先从本机 uv 缓存拷。每出一次包重下 56MB 没道理，而且下载一慢整条出包线就卡死——
# 这台机器 2026-09-22 就卡了 10 分钟。拷不到才退回下载。
CACHED="$(ls -d "$HOME/.local/share/uv/python/cpython-$PYVER".*-macos-"$ARCH"-none 2>/dev/null | head -1)"
if [ -n "$CACHED" ]; then
  echo "   用本机缓存 $(basename "$CACHED")"
  cp -Rp "$CACHED" "$STAGE$ROOT/python/"
else
  echo "   本机没缓存，下载"
  uv python install --install-dir "$STAGE$ROOT/python" "$PYVER" >/dev/null
fi

echo "== 4/6 建 venv 并装组件 =="
# --relocatable：venv 里的脚本用相对路径找解释器，这样它在打包机的 STAGE 下建、
# 在别人机器的 $ROOT 下跑，两边都对。
UV_PYTHON_INSTALL_DIR="$STAGE$ROOT/python" \
  uv venv --relocatable --managed-python --python "$PYVER" "$STAGE$ROOT/venv" >/dev/null
uv pip install --python "$STAGE$ROOT/venv/bin/python" "$WHEEL" >/dev/null

# uv 会把「安装目录」的绝对路径写进产物：软链的目标、pyvenv.cfg 的 home、
# 还有 _sysconfigdata 里的一串编译期路径。打包时那个目录是 $STAGE，到别人机器上
# 全是死链。这里不逐个点名（点名过一次就漏了 aarch64/arm64 命名那条），
# 而是把 $STAGE 这个前缀从整棵树里抹掉——抹完剩下的正好是目标机的绝对路径。
# --relocatable 只管 venv/bin 里的控制台脚本，管不到这些。
PYDIR="$(cd "$STAGE$ROOT/python" && ls -d cpython-*-macos-*-none/ | head -1)"
PYDIR="${PYDIR%/}"
[ -n "$PYDIR" ] || { echo "找不到装好的 Python 目录"; exit 1; }

find "$STAGE" -type l | while IFS= read -r link; do
  target="$(readlink "$link")"
  case "$target" in
    "$STAGE"*) ln -snf "${target#"$STAGE"}" "$link" ;;
  esac
done
grep -rIl -- "$STAGE" "$STAGE" 2>/dev/null | while IFS= read -r f; do
  LC_ALL=C sed -i '' "s|$STAGE||g" "$f"
done
ln -snf "$ROOT/python/$PYDIR/bin/python3.12" "$STAGE$ROOT/venv/bin/python"
sed -i '' "s|^home = .*|home = $ROOT/python/$PYDIR/bin|" "$STAGE$ROOT/venv/pyvenv.cfg"

# 抹掉 $STAGE 还不够：Python 不是在 $STAGE 里编出来的，是从 uv 缓存拷来的，
# _sysconfigdata 里那串编译期路径指的是缓存目录，也就是打包人的家目录。
# 发给别人的包里带着打包人的用户名，2026-09-22 被仓库那条「禁用标识词」守卫抓到。
UVCACHE="$HOME/.local/share/uv/python/$PYDIR"
grep -rIl -- "$UVCACHE" "$STAGE" 2>/dev/null | while IFS= read -r f; do
  LC_ALL=C sed -i '' "s|$UVCACHE|$ROOT/python/$PYDIR|g" "$f"
done

# pip 给「按本地路径装的 wheel」留的溯源文件，内容就是打包机上那个 wheel 的绝对路径。
# 纯元数据，删掉不影响运行；RECORD 里那行一并删，免得留下一条指向不存在文件的记录。
DIST="$STAGE$ROOT/venv/lib/python3.12/site-packages/ads_control_plane-$VERSION.dist-info"
rm -f "$DIST/direct_url.json"
[ -f "$DIST/RECORD" ] && sed -i '' '/direct_url\.json/d' "$DIST/RECORD"

echo "== 自检 =="
# 两条判据。第三条「链是断的」故意不查：指向 /Library/... 的链在打包机上本来就是断的，
# 装完才成立——把它当错误正好会把唯一正确的形态判死。
#   ① 任何文件内容里都不许再出现打包机路径，也不许出现打包人的家目录
#      （$STAGE 在 $HOME 底下，但 uv 缓存不在 $STAGE 底下——两条都要查）
#   ② 绝对软链只许指向 $ROOT 里面；相对软链必须落在包内
BAD=""
LEAK="$(grep -rIl -e "$STAGE" -e "$HOME" "$STAGE" 2>/dev/null || true)"
[ -n "$LEAK" ] && BAD="$BAD
内容里有打包机路径：
$LEAK"
while IFS= read -r link; do
  target="$(readlink "$link")"
  case "$target" in
    "$ROOT"/*) ;;                                     # 指向目标机的安装目录，对
    /*) BAD="$BAD
软链指到包外：$link -> $target" ;;
    *) # -e 会跟穿：python3 -> python，而 python 是指向 $ROOT 的绝对链，跟下去在打包机上是空的。
       # 只问「这个路径项在不在」，用 -L 兜住指向绝对链的那一跳。
       sib="$(dirname "$link")/$target"
       { [ -e "$sib" ] || [ -L "$sib" ]; } || BAD="$BAD
相对软链在包内找不到：$link -> $target" ;;
  esac
done <<EOF
$(find "$STAGE" -type l)
EOF
if [ -n "$(printf '%s' "$BAD" | tr -d '[:space:]')" ]; then
  echo "拒绝出包：$BAD"; exit 1
fi
echo "   内容干净，软链全部落在安装目录内。"

echo "== 5/6 写 postinstall =="
cat > "$SCRIPTS/postinstall" <<'POST'
#!/bin/bash
# 由安装器以 root 执行。真正的活全在 bootstrap 里，这里只负责调它并把失败说清楚。
set -euo pipefail
ROOT="/Library/Application Support/amazon-ads"
exec "$ROOT/venv/bin/python" -m ads_control_plane.sfw bootstrap
POST
chmod +x "$SCRIPTS/postinstall"

echo "== 6/6 pkgbuild =="
ARGS=(--root "$STAGE" --scripts "$SCRIPTS" --identifier "$IDENT" --version "$VERSION"
      --install-location / --ownership recommended)
[ -n "$SIGN" ] && ARGS+=(--sign "$SIGN")
pkgbuild "${ARGS[@]}" "$OUT"

echo
echo "好了：$OUT"
echo "大小：$(du -h "$OUT" | cut -f1)   架构：$ARCH"
[ -z "$SIGN" ] && echo "没签名：别人装的时候要右键『打开』，或到系统设置→隐私与安全性里放行。"
