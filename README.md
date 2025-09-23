# Funpay Telegram Stars (без KYC)   
🚀 Бот для автоматизации продаж звёзд на FunPay  
📌 Сейчас в стадии бета-тестирования
      
      
## Что из себя представляет бот?  
Это Python-скрипт, который:  
✔ Для бота не нужна верификация на Fragment.  
✔ 2% комиссия на покупку звезд.  
✔ Проверяет ники через Telegram.  
✔ Возвращает деньги при ошибках.  
  
## Что нужно для работы бота?  
1. Установка Python и библиотек
```pip install -r requirements.txt```
2. Зарегистироваться на [сайте](https://пополнистим.рф/)
3. Настройка .env
```
FUNPAY_AUTH_TOKEN=golden_key
API_USER=API_USER
API_PASS=API_PASS
API_ID=API_ID
API_HASH=API_HASH
COOLDOWN_SECONDS=1
AUTO_REFUND=true/false
AUTO_DEACTIVATE=true/false
```
4. Запустить файл ```create_session.py``` для создания сессии и делайте все, что написано в терминале.   

Есть другой бот по продаже звезд, для него нужна верификация [Бот](https://github.com/tinechelovec/Funpay-Telegram-Stars)
   
По всем багам, вопросам и предложениям пишите в [Issues](https://github.com/tinechelovec/Funpay-Telegram-Stars/issues) или в [Telegram](https://t.me/tinechelovec)

Другие боты и плагины [Channel](https://t.me/by_thc)
