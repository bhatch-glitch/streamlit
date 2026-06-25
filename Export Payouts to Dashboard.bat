@echo off
cd /d "%~dp0"
start "" "https://nextgen.creatoriq.com/#campaign/2405009/campaign_payouts"
echo.
echo 1. In CreatorIQ, open the payouts table for campaign 2405009
echo 2. Click Export / Download CSV
echo 3. Save the file into:
echo    %~dp0data\incoming\
echo.
echo The dashboard imports the newest file automatically every hour.
echo Or double-click "Start App.bat" and use Refresh in the sidebar.
pause
