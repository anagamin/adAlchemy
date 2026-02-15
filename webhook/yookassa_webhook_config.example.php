<?php
/**
 * Пример конфига. Скопируйте в yookassa_webhook_config.php и заполните.
 * Файл yookassa_webhook_config.php не должен попадать в репозиторий (добавьте в .gitignore).
 */
return [
    'db' => [
        'host' => 'localhost',
        'port' => 3306,
        'database' => 'adalechemy',
        'user' => 'your_mysql_user',
        'password' => 'your_mysql_password',
    ],
    'telegram_bot_token' => 'your_telegram_bot_token_from_BotFather',
];
