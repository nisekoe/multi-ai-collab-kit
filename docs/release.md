# 打包、发布与更新

## 从 Git 跟踪文件生成附件

在源码目录执行以下命令。

```bash
python tools/make_release.py --version 0.3.3 --out dist --manifest-out kit-manifest.json
```

`dist/kit-0.3.3.zip` 依据 Git 跟踪文件打包。`kit-manifest.json` 放在 ZIP 外，记录此附件的实际字节数和 SHA-256。先解包核对模板并跑离线测试，再把同一份 ZIP 上传到对应版本的 Release。核对上传资产后，将外置清单发布到主分支。更新器默认从那里读取它。不要手工修改 ZIP 内文件后沿用旧清单。

## 在解压安装目录外运行更新器

以下是 PowerShell 示例。将 `$kitDir` 和 `$dshVersion` 改为实际安装路径和 DSH 版本。每个安装目录使用独立的状态文件。

```powershell
$kitDir = (Resolve-Path "C:/tools/multi-ai-collab-kit").Path
$kitState = Join-Path (Split-Path -Parent $kitDir) "multi-ai-kit-version.json"
$dshVersion = "0.4.0"
Set-Location (Split-Path -Parent $kitDir)
python "$kitDir/tools/kit_update.py" --install-dir "$kitDir" --state-file "$kitState" --check --dsh-version "$dshVersion"
python "$kitDir/tools/kit_update.py" --install-dir "$kitDir" --state-file "$kitState" --apply --dsh-version "$dshVersion"
python "$kitDir/tools/kit_update.py" --install-dir "$kitDir" --state-file "$kitState" --status
python "$kitDir/tools/kit_update.py" --install-dir "$kitDir" --state-file "$kitState" --rollback
```

`--check` 只记录检查结果。`--apply` 和 `--rollback` 会询问确认。`--yes` 可用于已确认的操作。如果旧安装没有 `VERSION`，可用 `--installed-version` 指定当前版本。`--manifest-url` 可以指向自管的 HTTPS 清单。

## 检查与回滚

更新器验证 HTTPS、附件大小、SHA-256 和 DSH 最低版本。通过临时目录解包与自检后再切换。上一版留作回滚，从已安装目录之外运行更新和回滚。清单当前通过可信 HTTPS 获取，尚未提供独立签名。离线更新测试见 `tests/test_kit_update.py`。
