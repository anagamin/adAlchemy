<?php
/**
 * Вебхук YooKassa для пополнения баланса AdAlechemy.
 * Залить на nginx, указать в личном кабинете YooKassa URL: https://ваш-домен/путь/yookassa_webhook.php
 * Создать yookassa_webhook_config.php из yookassa_webhook_config.example.php и заполнить данные.
 */

header('Content-Type: application/json; charset=utf-8');

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['error' => 'Method not allowed']);
    exit;
}

$configFile = __DIR__ . '/yookassa_webhook_config.php';
if (!is_file($configFile)) {
    http_response_code(500);
    echo json_encode(['error' => 'Config not found']);
    exit;
}

$config = require $configFile;
$db = $config['db'] ?? null;
$telegramToken = $config['telegram_bot_token'] ?? '';

if (!$db || empty($db['host']) || empty($db['database'])) {
    http_response_code(500);
    echo json_encode(['error' => 'Invalid config']);
    exit;
}

$raw = file_get_contents('php://input');
$data = json_decode($raw, true);
if (!is_array($data)) {
    http_response_code(400);
    echo json_encode(['error' => 'Invalid JSON']);
    exit;
}

$event = $data['event'] ?? '';
if ($event !== 'payment.succeeded') {
    echo json_encode(['status' => 'ignored']);
    exit;
}

$obj = $data['object'] ?? [];
$paymentId = $obj['id'] ?? null;
if (!$paymentId || !is_string($paymentId)) {
    http_response_code(400);
    echo json_encode(['error' => 'Missing object.id']);
    exit;
}

try {
    $pdo = new PDO(
        sprintf(
            'mysql:host=%s;port=%s;dbname=%s;charset=utf8mb4',
            $db['host'],
            $db['port'] ?? 3306,
            $db['database']
        ),
        $db['user'] ?? '',
        $db['password'] ?? '',
        [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]
    );
} catch (PDOException $e) {
    http_response_code(500);
    echo json_encode(['error' => 'DB connection failed']);
    exit;
}

$stmt = $pdo->prepare(
    'SELECT id, user_id, telegram_id, amount_rub, status FROM payments WHERE yookassa_payment_id = ?'
);
$stmt->execute([$paymentId]);
$row = $stmt->fetch(PDO::FETCH_ASSOC);

if (!$row) {
    echo json_encode(['status' => 'ok']);
    exit;
}

if ($row['status'] !== 'pending') {
    echo json_encode(['status' => 'ok']);
    exit;
}

$telegramId = (int) $row['telegram_id'];
$amount = (float) $row['amount_rub'];

$pdo->beginTransaction();
try {
    $upd = $pdo->prepare(
        "UPDATE payments SET status = 'succeeded' WHERE yookassa_payment_id = ? AND status = 'pending'"
    );
    $upd->execute([$paymentId]);
    if ($upd->rowCount() === 0) {
        $pdo->rollBack();
        echo json_encode(['status' => 'ok']);
        exit;
    }
    $pdo->prepare('UPDATE users SET balance = balance + ? WHERE telegram_id = ?')->execute([$amount, $telegramId]);
    $pdo->commit();
} catch (Exception $e) {
    $pdo->rollBack();
    http_response_code(500);
    echo json_encode(['error' => 'DB update failed']);
    exit;
}

if ($telegramToken !== '') {
    $text = 'Баланс успешно пополнен на ' . number_format($amount, 2, '.', '') . ' ₽. Спасибо!';
    $url = 'https://api.telegram.org/bot' . $telegramToken . '/sendMessage';
    $ctx = stream_context_create([
        'http' => [
            'method' => 'POST',
            'header' => 'Content-Type: application/json',
            'content' => json_encode([
                'chat_id' => $telegramId,
                'text' => $text,
            ]),
            'timeout' => 10,
        ],
    ]);
    @file_get_contents($url, false, $ctx);
}

echo json_encode(['status' => 'ok']);
