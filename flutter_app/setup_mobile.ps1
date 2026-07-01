# PersonalOS / Amy mobile — one-shot setup (Windows PowerShell)
# Run from the flutter_app folder:  powershell -ExecutionPolicy Bypass -File .\setup_mobile.ps1
#
# Does the 3 setup steps you can't skip:
#   1. flutter create .  (generates android/ ios/) + patches Android permissions & share intent
#   2. flutter pub get
#   3. installs the backend's new dep (python-multipart) for photo upload

$ErrorActionPreference = "Stop"
Write-Host "== PersonalOS mobile setup ==" -ForegroundColor Cyan

# Always run from the flutter_app folder (where this script lives), no matter
# what directory you invoke it from.
Set-Location -Path $PSScriptRoot
Write-Host "Working in: $PSScriptRoot" -ForegroundColor DarkGray

# --- 1. generate platform folders (android + ios) ---------------------------
if (-not (Test-Path ".\android") -or -not (Test-Path ".\ios")) {
    Write-Host "[1/5] flutter create --platforms=android,ios . (generating android/ ios/ ...)" -ForegroundColor Yellow
    flutter create --org com.personalos --project-name amy_app --platforms=android,ios .
} else {
    Write-Host "[1/5] android/ and ios/ already exist - skipping flutter create" -ForegroundColor Green
}

# --- 2. patch AndroidManifest.xml -------------------------------------------
$manifest = ".\android\app\src\main\AndroidManifest.xml"
if (Test-Path $manifest) {
    Write-Host "[2/5] patching $manifest" -ForegroundColor Yellow
    $xml = Get-Content $manifest -Raw

    $perms = @(
        'android.permission.INTERNET',
        'android.permission.CAMERA',
        'android.permission.ACCESS_FINE_LOCATION',
        'android.permission.ACCESS_COARSE_LOCATION',
        'android.permission.READ_MEDIA_IMAGES',
        'android.permission.READ_EXTERNAL_STORAGE',
        'android.permission.ACCESS_MEDIA_LOCATION'
    )
    $permBlock = ($perms | ForEach-Object { "    <uses-permission android:name=`"$_`"/>" }) -join "`r`n"

    if ($xml -notmatch 'android.permission.CAMERA') {
        $xml = $xml -replace '(<application)', "$permBlock`r`n`r`n`$1"
    }
    # allow http:// (LAN) calls
    if ($xml -notmatch 'usesCleartextTraffic') {
        $xml = $xml -replace '(<application)', ('$1' + "`r`n        android:usesCleartextTraffic=`"true`"")
    }
    # share-to-Amy intent filters (capture mode C) on the launcher activity
    if ($xml -notmatch 'android.intent.action.SEND') {
        $share = @"
            <intent-filter>
                <action android:name="android.intent.action.SEND"/>
                <category android:name="android.intent.category.DEFAULT"/>
                <data android:mimeType="image/*"/>
            </intent-filter>
            <intent-filter>
                <action android:name="android.intent.action.SEND_MULTIPLE"/>
                <category android:name="android.intent.category.DEFAULT"/>
                <data android:mimeType="image/*"/>
            </intent-filter>
"@
        # insert before the FIRST </activity> (the main activity)
        $idx = $xml.IndexOf('</activity>')
        if ($idx -ge 0) { $xml = $xml.Insert($idx, "$share`r`n        ") }
    }
    Set-Content $manifest $xml -Encoding UTF8
    Write-Host "      permissions + cleartext + share intent added" -ForegroundColor Green
} else {
    Write-Host "      manifest not found (did flutter create run?) - skipping" -ForegroundColor Red
}

# --- 3. patch iOS Info.plist ------------------------------------------------
$plist = ".\ios\Runner\Info.plist"
if (Test-Path $plist) {
    Write-Host "[3/5] patching $plist" -ForegroundColor Yellow
    $p = Get-Content $plist -Raw

    $iosKeys = @"
	<key>NSCameraUsageDescription</key>
	<string>Take photos to save into your PersonalOS vault.</string>
	<key>NSPhotoLibraryUsageDescription</key>
	<string>Pick or sync photos into your PersonalOS vault.</string>
	<key>NSLocationWhenInUseUsageDescription</key>
	<string>Tag captures with where they were taken.</string>
	<key>NSAppTransportSecurity</key>
	<dict>
		<key>NSAllowsArbitraryLoads</key>
		<true/>
	</dict>
"@
    if ($p -notmatch 'NSCameraUsageDescription') {
        # insert the keys just before the final </dict> of the plist
        $idx = $p.LastIndexOf('</dict>')
        if ($idx -ge 0) { $p = $p.Insert($idx, $iosKeys); Set-Content $plist $p -Encoding UTF8 }
        Write-Host "      camera/photo/location usage + ATS (http) added" -ForegroundColor Green
    } else {
        Write-Host "      iOS keys already present - skipping" -ForegroundColor Green
    }
} else {
    Write-Host "[3/5] ios/Runner/Info.plist not found - skipping (run on a Mac to build iOS)" -ForegroundColor Red
}

# --- 4. flutter deps --------------------------------------------------------
Write-Host "[4/5] flutter pub get" -ForegroundColor Yellow
flutter pub get

# --- 5. backend dep ---------------------------------------------------------
Write-Host "[5/5] installing backend dep (python-multipart)" -ForegroundColor Yellow
try { pip install python-multipart } catch { Write-Host "      pip failed - run 'pip install python-multipart' in your backend env" -ForegroundColor Red }

Write-Host ""
Write-Host "Done." -ForegroundColor Cyan
Write-Host "Android: ready - run 'flutter run' on a device/emulator."
Write-Host "iOS:     project + Info.plist are ready, but BUILDING iOS needs a Mac with Xcode."
Write-Host "         Share-to-Amy on iOS also needs a Share Extension target added in Xcode"
Write-Host "         (see receive_sharing_intent docs) - camera/gallery/sync work without it."
Write-Host ""
Write-Host "  - Start backend:  cd ..; python main.py --mode personal --host 0.0.0.0 --port 8848"
Write-Host "  - Run app:        flutter run   (then set Server URL in Settings)"
