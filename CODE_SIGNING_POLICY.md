# 代码签名政策 / Code signing policy

Free code signing provided by [SignPath.io](https://signpath.io), certificate by [SignPath Foundation](https://signpath.org).

（申请中：获批之前，发布的安装包和免安装版都没有签名。Pending approval: releases are unsigned until then.）

## 团队角色 / Team roles

| 角色 Role | 成员 Members |
|---|---|
| 提交者和审核者 Committers and reviewers | [@Joker0320](https://github.com/Joker0320) |
| 签名审批 Approvers | [@Joker0320](https://github.com/Joker0320) |

所有成员在 GitHub 和 SignPath 上都开启了双重验证（2FA）。
All team members use multi-factor authentication for GitHub and SignPath.

## 签名范围 / What is signed

只签名本仓库源码经 GitHub Actions（`.github/workflows/release.yml`）构建出的 `KOI BT.exe`、安装包和其中的程序文件。
Only binaries built from this repository by GitHub Actions are signed.

## 隐私 / Privacy

见 [PRIVACY.md](PRIVACY.md)。See [PRIVACY.md](PRIVACY.md).
