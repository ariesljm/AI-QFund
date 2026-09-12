"""AI-QFund 包入口：统一强制"完全无代理"运行。

本项目访问的全部是国内站点（东财/腾讯/搜狐/新浪），必须直连——
目标部署环境无代理；开发机即使开着代理工具（如 HTTP_PROXY=127.0.0.1:xxxx）
也不能误走（绕路且代理软件未启动时全部请求失败）。

curl_cffi / curl.exe / urllib / requests 都会读 http_proxy/https_proxy 环境
变量（httpx 已有 trust_env=False，tls_client 原生直连，无需处理）；在包
导入最早处统一清除代理变量并设 NO_PROXY=*，保证所有降级链一律直连，
与部署环境行为一致。
"""

import os as _os

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
           "http_proxy", "https_proxy", "all_proxy"):
    _os.environ.pop(_k, None)
_os.environ["NO_PROXY"] = "*"
_os.environ["no_proxy"] = "*"