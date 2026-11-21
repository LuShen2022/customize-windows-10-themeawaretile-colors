# ============================================
# StartUI.dll 自动替换工具
# 包含 DLL 替换、SHA256 验证、DACL 权限还原
# ============================================

$ErrorActionPreference = 'Stop'

$targetDir = 'C:\Windows\SystemApps\Microsoft.Windows.StartMenuExperienceHost_cw5n1h2txyewy'
$targetDll = Join-Path $targetDir 'StartUI.dll'

# ============================================
# 1. 管理员权限检查
# ============================================

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)

if (-not $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)) {
    Write-Host '请使用管理员身份运行 PowerShell！' -ForegroundColor Red
    Read-Host '按回车退出'
    exit
}

Write-Host '============================================'
Write-Host '       StartUI.dll 自动替换工具'
Write-Host '============================================'

# ============================================
# 2. 要求用户手动备份原 DLL
# ============================================

Write-Host ''
Write-Host '请先手动将原始 StartUI.dll 复制到安全位置。'
Write-Host '确认备份完成后再继续。'

$backupConfirm = Read-Host '已完成备份？输入 Y 继续'

if ($backupConfirm -notmatch '^[Yy]$') {
    exit
}

# ============================================
# 3. 输入新 DLL 路径
# ============================================

$newDll = (Read-Host "`n请输入新 StartUI.dll 的完整路径").Trim().Trim('"')

if (-not (Test-Path -LiteralPath $newDll -PathType Leaf)) {
    Write-Host '新 DLL 不存在！' -ForegroundColor Red
    Read-Host '按回车退出'
    exit
}

if (-not (Test-Path -LiteralPath $targetDll -PathType Leaf)) {
    Write-Host '原始 DLL 不存在！' -ForegroundColor Red
    Read-Host '按回车退出'
    exit
}

if ((Resolve-Path $newDll).Path -eq (Resolve-Path $targetDll).Path) {
    Write-Host '新 DLL 和目标 DLL 是同一个文件！' -ForegroundColor Red
    Read-Host '按回车退出'
    exit
}

Write-Host "`n========== 新 DLL =========="
Write-Host "路径：$newDll"
Write-Host "版本：$((Get-Item $newDll).VersionInfo.FileVersion)"

Write-Host "`n========== 当前 DLL =========="
Write-Host "路径：$targetDll"
Write-Host "版本：$((Get-Item $targetDll).VersionInfo.FileVersion)"

# ============================================
# 4. 生成旧 DLL 备份名称
# ============================================

$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$oldDll = Join-Path $targetDir "StartUI_old_$timestamp.dll"

if (Test-Path -LiteralPath $oldDll) {
    Write-Host '备份文件名冲突，停止操作。' -ForegroundColor Red
    Read-Host '按回车退出'
    exit
}

Write-Host "`n============================================"
Write-Host '即将获取原 DLL 权限并执行替换！'
Write-Host '替换后将自动还原新 DLL 的 DACL 权限。'
Write-Host '============================================'

$confirm = Read-Host '输入 Y 继续'

if ($confirm -notmatch '^[Yy]$') {
    exit
}

# ============================================
# 5. 定义 MoveFileEx
# ============================================

if (-not ('NativeMethods' -as [type])) {
    Add-Type @'
using System;
using System.Runtime.InteropServices;

public static class NativeMethods {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode,
        SetLastError = true)]
    public static extern bool MoveFileEx(
        string lpExistingFileName,
        string lpNewFileName,
        int dwFlags
    );
}
'@
}

# ============================================
# 6. 获取原 DLL 操作权限
# ============================================

Write-Host "`n[1/6] 正在获取原 DLL 的操作权限..."

& takeown.exe /f $targetDll /a

if ($LASTEXITCODE -ne 0) {
    throw '获取原 DLL 所有权失败。'
}

& icacls.exe $targetDll /grant '*S-1-5-32-544:(F)'

if ($LASTEXITCODE -ne 0) {
    throw '授予 Administrators 完全控制权限失败。'
}

# ============================================
# 7. 关闭开始菜单进程
# ============================================

Write-Host "`n[2/6] 正在关闭开始菜单进程..."

Get-Process StartMenuExperienceHost -ErrorAction SilentlyContinue |
    Stop-Process -Force

$deadline = (Get-Date).AddSeconds(5)

while (
    (Get-Process StartMenuExperienceHost -ErrorAction SilentlyContinue) -and
    (Get-Date) -lt $deadline
) {
    Start-Sleep -Milliseconds 100
}

if (Get-Process StartMenuExperienceHost -ErrorAction SilentlyContinue) {
    throw '开始菜单进程未能退出，停止替换。'
}

# ============================================
# 8. 改名旧 DLL
# ============================================

Write-Host "`n[3/6] 正在使用 MoveFileEx 改名旧 DLL..."

$result = [NativeMethods]::MoveFileEx($targetDll, $oldDll, 0)

if (-not $result) {
    $code = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
    throw "MoveFileEx 改名失败，错误代码：$code"
}

Write-Host "旧 DLL 已改名：$oldDll" -ForegroundColor Green

# ============================================
# 9. 复制新 DLL 并还原权限
# ============================================

try {
    Write-Host "`n[4/6] 正在复制新 DLL..."

    Copy-Item -LiteralPath $newDll -Destination $targetDll -ErrorAction Stop

    if (-not (Test-Path -LiteralPath $targetDll -PathType Leaf)) {
        throw '复制后未找到目标 DLL。'
    }

    # SHA256 验证
    $sourceHash = (Get-FileHash -LiteralPath $newDll -Algorithm SHA256).Hash
    $targetHash = (Get-FileHash -LiteralPath $targetDll -Algorithm SHA256).Hash

    if ($sourceHash -ne $targetHash) {
        throw '新 DLL 的 SHA256 校验不一致。'
    }

    Write-Host 'SHA256 校验通过。' -ForegroundColor Green

    # ========================================
    # 10. 还原原 DLL 的 DACL
    # ========================================

    Write-Host "`n[5/6] 正在还原新 DLL 的权限..."

    # 移除从父目录继承的权限
    & icacls.exe $targetDll /inheritance:r

    if ($LASTEXITCODE -ne 0) {
        throw '移除继承权限失败。'
    }

    # 原 DLL 的访问控制规则
    $rules = @(
        '*S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464:(F)'
        '*S-1-5-18:(RX)'
        '*S-1-5-32-544:(RX)'
        '*S-1-5-32-545:(RX)'
        '*S-1-15-2-1:(RX)'
        '*S-1-15-2-2:(RX)'
    )

    & icacls.exe $targetDll /grant:r $rules

    if ($LASTEXITCODE -ne 0) {
        throw '还原新 DLL 的 DACL 失败。'
    }

    Write-Host '新 DLL 的 DACL 还原成功！' -ForegroundColor Green

}
catch {
    Write-Host "`n替换或权限还原失败，正在尝试恢复原 DLL..." -ForegroundColor Red

    # 删除新 DLL
    if (Test-Path -LiteralPath $targetDll) {
        try {
            Remove-Item -LiteralPath $targetDll -Force -ErrorAction Stop
            Write-Host '失败的新 DLL 已清理。'
        }
        catch {
            Write-Host "新 DLL 清理失败：$($_.Exception.Message)" -ForegroundColor Red
        }
    }

    # 恢复旧 DLL
    if (
        (Test-Path -LiteralPath $oldDll) -and
        (-not (Test-Path -LiteralPath $targetDll))
    ) {
        $restore = [NativeMethods]::MoveFileEx($oldDll, $targetDll, 0)

        if ($restore) {
            Write-Host '原 DLL 已恢复。' -ForegroundColor Green
        }
        else {
            $code = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            Write-Host "恢复失败，错误代码：$code" -ForegroundColor Red
            Write-Host "旧 DLL 仍保留在：$oldDll"
        }
    }
    else {
        Write-Host "请检查现场文件，旧 DLL 位置：$oldDll" -ForegroundColor Yellow
    }

    throw $_
}

# ============================================
# 11. 删除旧 DLL
# ============================================

Write-Host "`n[6/6] 正在删除改名后的旧 DLL..."

try {
    Remove-Item -LiteralPath $oldDll -Force -ErrorAction Stop
    Write-Host '旧 DLL 已删除。' -ForegroundColor Green
}
catch {
    Write-Host "旧 DLL 删除失败，仍保留在：$oldDll" -ForegroundColor Yellow
    Write-Host $_.Exception.Message
}

# ============================================
# 12. 等待开始菜单进程重新启动
# ============================================

Write-Host "`n正在等待 Windows 重新启动开始菜单进程..."

Start-Sleep -Seconds 2

if (Get-Process StartMenuExperienceHost -ErrorAction SilentlyContinue) {
    Write-Host '开始菜单进程已经重新启动。' -ForegroundColor Green
}
else {
    Write-Host '当前未检测到开始菜单进程，Windows 可能仍在重新启动它。' -ForegroundColor Yellow
}

# ============================================
# 13. 显示最终权限
# ============================================

Write-Host "`n========== 最终 DLL 权限 =========="
icacls.exe $targetDll

Write-Host "`n========== 最终文件所有者 =========="
(Get-Acl -LiteralPath $targetDll).Owner

Write-Host "`n============================================"
Write-Host 'StartUI.dll 替换流程结束！'
Write-Host '============================================'

Read-Host '按回车退出'