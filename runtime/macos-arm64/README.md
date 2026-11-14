# runtime/macos-arm64 — macOS (Apple Silicon) 预编译运行库

本目录提供在 macOS arm64(Apple Silicon)上直接运行本 MCP 所需的两个本地库,
免安装 Homebrew,下载仓库后即可用环境变量指向它们。

## 文件

| 文件 | 说明 | 来源 |
|---|---|---|
| `libusb-1.0.dylib` | libusb 1.0.29,arm64,x86_64 需另行编译 | [libusb/libusb](https://github.com/libusb/libusb) v1.0.29 源码编译 |
| `libsi104_sdk.1.0.5.dylib` | SI104 SDK 原生库 1.0.5,arm64 | Lanmotion SDK 1.0.5 `cpp/` 源码 CMake 编译 |
| `libsi104_sdk.1.dylib` / `libsi104_sdk.dylib` | 常规 so-name 符号链接 | 指向上者 |

SHA256 见 `SHA256SUMS`。

## 使用

```bash
R=<仓库路径>/runtime/macos-arm64
export SI104_SDK_LIBRARY=$R/libsi104_sdk.dylib
export SI104_LIBUSB=$R/libusb-1.0.dylib
SI104_MCP_TRANSPORT=http .venv/bin/python server.py
```

Python 绑定无需额外安装:`PYTHONPATH=<仓库路径>/../sdk-extract/python`(或
`pip install python/wheels/utools_si104-1.0.5-py3-none-any.whl`)。

首次运行若被 macOS Gatekeeper 拦截:

```bash
xattr -dr com.apple.quarantine runtime/macos-arm64/*.dylib
```

## 复现编译

```bash
# libusb 1.0.29(macOS 无 udev,最小配置)
cmake -S libusb -B build/libusb -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=ON -DLIBUSB_BUILD_EXAMPLES=OFF -DLIBUSB_BUILD_TESTING=OFF
cmake --build build/libusb

# SDK 1.0.5
cmake -S sdk-extract/cpp -B build/sdk -DCMAKE_BUILD_TYPE=Release
cmake --build build/sdk
```

## 许可

- `libusb-1.0.dylib`:LGPL-2.1-or-later(上游源码见 libusb 仓库;SDK 包 `licenses/` 内含对应上游源码副本)
- `libsi104_sdk.1.0.5.dylib`:由 Lanmotion UTools-SI104 SDK 1.0.5 源码编译,授权遵循该 SDK 随附许可
