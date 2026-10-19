@echo off
rem Ставит задание в Планировщик Windows: каждый день в 06:00.
set DIR=%~dp0..
schtasks /Create /F /SC DAILY /ST 06:00 /TN "WbOzon daily export" /TR "cmd /c cd /d \"%DIR%\" && python run.py"
echo Готово.
