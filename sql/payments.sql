-- Платежи YooKassa для пополнения баланса

CREATE TABLE IF NOT EXISTS payments (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    yookassa_payment_id VARCHAR(64) NOT NULL UNIQUE COMMENT 'ID платежа в YooKassa',
    user_id INT UNSIGNED NOT NULL,
    telegram_id BIGINT NOT NULL,
    amount_rub DECIMAL(12, 2) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending' COMMENT 'pending, succeeded, canceled',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_yookassa_payment_id (yookassa_payment_id),
    INDEX idx_telegram_status (telegram_id, status),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
