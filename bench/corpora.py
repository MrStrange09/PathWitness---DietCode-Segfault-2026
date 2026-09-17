CORPORA = {
    "sqlite": dict(
        url="https://www.sqlite.org/2024/sqlite-amalgamation-3450100.zip",
        sha256="5592243caf28b2cdef41e6ab58d25d653dfc53deded8450eb66072c929f030c4",
        directory="sqlite-amalgamation-3450100",
        sources=["sqlite3.c", "shell.c"],
        flags=["-std=gnu17"]),
    "zlib": dict(
        url="https://zlib.net/fossils/zlib-1.3.1.tar.gz",
        sha256="9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23",
        directory="zlib-1.3.1",
        sources=["adler32.c", "compress.c", "crc32.c", "deflate.c", "gzclose.c",
                 "gzlib.c", "gzread.c", "gzwrite.c", "infback.c", "inffast.c",
                 "inflate.c", "inftrees.c", "trees.c", "uncompr.c", "zutil.c"],
        flags=["-std=gnu17", "-DHAVE_UNISTD_H"]),
    "lua": dict(
        url="https://www.lua.org/ftp/lua-5.4.6.tar.gz",
        sha256="7d5ea1b9cb6aa0b59ca3dde1c6adcb57ef83a1ba8e5432c0ecd06bf439b3ad88",
        directory="lua-5.4.6",
        sources=["src/*.c"],
        flags=["-std=gnu99", "-DLUA_COMPAT_5_3", "-DLUA_USE_LINUX"]),
    "cjson": dict(
        url="https://github.com/DaveGamble/cJSON/archive/refs/tags/v1.7.18.tar.gz",
        sha256="3aa806844a03442c00769b83e99970be70fbef03735ff898f4811dd03b9f5ee5",
        directory="cJSON-1.7.18",
        sources=["cJSON.c", "cJSON_Utils.c"],
        flags=["-std=gnu17"]),
    "bzip2": dict(
        url="https://sourceware.org/pub/bzip2/bzip2-1.0.8.tar.gz",
        sha256="ab5a03176ee106d3f0fa90e381da478ddae405918153cca248e682cd0c4a2269",
        directory="bzip2-1.0.8",
        sources=["blocksort.c", "huffman.c", "crctable.c", "randtable.c",
                 "compress.c", "decompress.c", "bzlib.c"],
        flags=["-std=gnu17"]),
    "lz4": dict(
        url="https://github.com/lz4/lz4/archive/refs/tags/v1.9.4.tar.gz",
        sha256="0b0e3aa07c8c063ddf40b082bdf7e37a1562bda40a0ff5272957f3e987e0e54b",
        directory="lz4-1.9.4",
        sources=["lib/*.c"],
        flags=["-std=gnu17"]),
    "miniz": dict(
        url="https://github.com/richgel999/miniz/archive/refs/tags/3.0.2.tar.gz",
        sha256="c4b4c25a4eb81883448ff8924e6dba95c800094a198dc9ce66a292ac2ef8e018",
        directory="miniz-3.0.2",
        sources=["miniz.c", "miniz_zip.c", "miniz_tinfl.c", "miniz_tdef.c"],
        flags=["-std=gnu17", "-D_GNU_SOURCE"]),
    "zip": dict(
        url="https://github.com/kuba--/zip/archive/refs/tags/v0.3.2.tar.gz",
        sha256="0c33740aec7a3913bca07df360420c19cac5e794e0f602f14f798cb2e6f710e5",
        directory="zip-0.3.2",
        sources=["src/*.c"],
        flags=["-std=gnu17"]),
}


def resolve(corpus, name):
    """(base directory, list of source files, flags) for an available project."""
    spec = CORPORA[name]
    base = corpus / spec["directory"]
    if not base.is_dir():
        return None, [], []
    files = []
    for pattern in spec["sources"]:
        if any(ch in pattern for ch in "*?["):
            files.extend(sorted(base.glob(pattern)))
        elif (base / pattern).is_file():
            files.append(base / pattern)
    return base, files, spec["flags"]


def fetch(corpus, name):
    """Download and unpack one benchmark, refusing anything but the pinned bytes."""
    import hashlib, io, tarfile, urllib.request, zipfile
    spec = CORPORA[name]
    base = corpus / spec["directory"]
    if base.is_dir():
        return base
    corpus.mkdir(parents=True, exist_ok=True)
    print(f"  fetching {name} {spec['url'].rsplit('/', 1)[-1]}", flush=True)
    with urllib.request.urlopen(spec["url"], timeout=300) as response:
        blob = response.read()
    actual = hashlib.sha256(blob).hexdigest()
    if actual != spec["sha256"]:
        raise RuntimeError(f"{name}: checksum mismatch\n  expected {spec['sha256']}\n"
                           f"  got      {actual}")
    if spec["url"].endswith(".zip"):
        zipfile.ZipFile(io.BytesIO(blob)).extractall(corpus)
    else:
        tarfile.open(fileobj=io.BytesIO(blob)).extractall(corpus, filter="data")
    return base
