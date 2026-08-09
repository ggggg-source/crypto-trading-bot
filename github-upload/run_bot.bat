@echo off
:start
echo Запускаю бота... %date% %time%
python bot.py
echo.
echo [!] Бот остановился. Перезапуск через 5 секунд...
timeout /t 5
goto start