# 打包与发布

## 从 Git 跟踪文件生成发布包

在仓库根目录执行（`--version` 需与 `VERSION` 文件一致）：

```bash
python tools/make_release.py --version 0.5.0 --out dist --manifest-out kit-manifest.json
```

`dist/kit-<版本>.zip` 按 Git 跟踪文件列表打包，自动排除 `.git`、`__pycache__`、`dist` 等目录。`kit-manifest.json` 生成在 ZIP 之外，记录附件的实际字节数与 SHA-256 校验值。

建议发布前先将生成的 ZIP 解压到干净临时目录，核对模板并执行：

```bash
python run_tests.py
python -m pip install .
```

验证通过后再打 Git Tag 并上传 Release 附件。

## 用户安装与升级方式

- **从源码 / Git**：`git pull` 后在虚拟环境中重新执行 `python -m pip install .`。
- **从 Release 附件**：下载并解压 `kit-<版本>.zip`，核对 `kit-manifest.json` 中的 SHA-256，然后运行 `python run_tests.py` 验证。
