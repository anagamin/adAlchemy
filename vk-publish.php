<?php
/**
 * Callback for "Опубликовать в ВК" from AdAlechemy bot.
 * Decodes JWT payload (campaign data), runs VK OAuth, creates one campaign in user's VK Ads cabinet.
 *
 * Configure: VK_APP_ID, VK_APP_SECRET, PUBLISH_JWT_SECRET.
 * Redirect URI in VK app must be: https://feedcraft.ru/vk-publish.php (or your base URL + /vk-publish.php)
 */

declare(strict_types=1);

session_start();

$VK_APP_ID       = getenv('VK_APP_ID')       ?: '';
$VK_APP_SECRET   = getenv('VK_APP_SECRET')   ?: '';
$JWT_SECRET      = getenv('PUBLISH_JWT_SECRET') ?: '';
$BASE_URL        = getenv('PUBLISH_BASE_URL') ?: 'https://feedcraft.ru';
$VK_API_VERSION  = '5.131';

function jsonResponse(array $data): void {
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($data, JSON_UNESCAPED_UNICODE);
    exit;
}

function htmlPage(string $title, string $body): void {
    header('Content-Type: text/html; charset=utf-8');
    echo '<!DOCTYPE html><html><head><meta charset="utf-8"><title>' . htmlspecialchars($title) . '</title></head><body>' . $body . '</body></html>';
    exit;
}

function decodeJwt(string $token, string $secret): ?array {
    $parts = explode('.', $token);
    if (count($parts) !== 3) return null;
    $payload = $parts[1];
    $payload = str_replace(['-', '_'], ['+', '/'], $payload);
    $payload = base64_decode($payload, true);
    if ($payload === false) return null;
    $sig = hash_hmac('sha256', $parts[0] . '.' . $parts[1], $secret, true);
    $sigB64 = str_replace(['+', '/', '='], ['-', '_', ''], base64_encode($sig));
    if (!hash_equals($sigB64, $parts[2])) return null;
    $data = json_decode($payload, true);
    if (!is_array($data)) return null;
    if (isset($data['exp']) && (int)$data['exp'] < time()) return null;
    return $data;
}

function vkApi(string $method, array $params, string $accessToken, string $v): array {
    $params['access_token'] = $accessToken;
    $params['v'] = $v;
    $url = 'https://api.vk.com/method/' . $method . '?' . http_build_query($params);
    $ctx = stream_context_create(['http' => ['timeout' => 15]]);
    $raw = @file_get_contents($url, false, $ctx);
    if ($raw === false) return ['error' => 'request_failed'];
    $json = json_decode($raw, true);
    if (!is_array($json)) return ['error' => 'invalid_response'];
    if (isset($json['error'])) return ['error' => $json['error']['error_msg'] ?? 'vk_error', 'error_code' => $json['error']['error_code'] ?? 0];
    return $json;
}

$payloadParam = $_GET['payload'] ?? '';
if ($payloadParam === '') {
    htmlPage('Ошибка', '<p>Не указан параметр payload.</p>');
}

$decoded = decodeJwt($payloadParam, $JWT_SECRET);
if ($decoded === null || empty($decoded['data'])) {
    htmlPage('Ошибка', '<p>Недействительная или просроченная ссылка. Сгенерируйте объявление заново в боте.</p>');
}

$data = $decoded['data'];
$redirectUri = rtrim($BASE_URL, '/') . '/vk-publish.php';

if (!empty($_GET['code'])) {
    $code = $_GET['code'];
    $state = $_GET['state'] ?? '';
    $tokenUrl = 'https://oauth.vk.com/access_token?' . http_build_query([
        'client_id'     => $VK_APP_ID,
        'client_secret' => $VK_APP_SECRET,
        'redirect_uri'  => $redirectUri,
        'code'          => $code,
    ]);
    $ctx = stream_context_create(['http' => ['timeout' => 10]]);
    $raw = @file_get_contents($tokenUrl, false, $ctx);
    $tokenData = $raw ? json_decode($raw, true) : null;
    if (is_array($tokenData) && !empty($tokenData['access_token'])) {
        $_SESSION['vk_ads_token'] = $tokenData['access_token'];
        $nextPayload = $state !== '' ? $state : $payloadParam;
        header('Location: ' . $redirectUri . '?payload=' . urlencode($nextPayload));
        exit;
    }
    htmlPage('Ошибка входа', '<p>Не удалось получить токен VK. Попробуйте ещё раз.</p>');
}

$accessToken = $_SESSION['vk_ads_token'] ?? '';

if ($accessToken === '') {
    $authUrl = 'https://oauth.vk.com/authorize?' . http_build_query([
        'client_id'     => $VK_APP_ID,
        'redirect_uri'  => $redirectUri,
        'response_type' => 'code',
        'scope'         => 'ads',
        'v'             => $VK_API_VERSION,
        'state'         => $payloadParam,
    ], '', '&', PHP_QUERY_RFC3986);
    htmlPage(
        'Вход ВКонтакте',
        '<p>Чтобы создать рекламную кампанию в вашем кабинете ВКонтакте, войдите:</p>' .
        '<p><a href="' . htmlspecialchars($authUrl) . '">Войти через VK и создать кампанию</a></p>'
    );
}

$accounts = vkApi('ads.getAccounts', [], $accessToken, $VK_API_VERSION);
if (!empty($accounts['error'])) {
    if (($accounts['error_code'] ?? 0) === 5) {
        unset($_SESSION['vk_ads_token']);
        header('Location: ' . $redirectUri . '?payload=' . urlencode((string)$payloadParam));
        exit;
    }
    htmlPage('Ошибка', '<p>Ошибка VK: ' . htmlspecialchars($accounts['error']) . '</p>');
}

$accountId = null;
if (!empty($accounts['response']) && is_array($accounts['response'])) {
    $first = reset($accounts['response']);
    $accountId = $first['account_id'] ?? $first['id'] ?? null;
}
if ($accountId === null) {
    htmlPage('Ошибка', '<p>Рекламный кабинет ВКонтакте не найден. Создайте кабинет в <a href="https://vk.com/ads">vk.com/ads</a>.</p>');
}

$campaignName = $data['campaign_name'] ?? 'Кампания AdAlechemy';
$dayLimit     = (string)($data['day_limit'] ?? 50000);
$allLimit     = (string)($data['all_limit'] ?? '0');
$linkUrl      = $data['link_url'] ?? 'https://vk.com';
$bid          = (string)($data['bid'] ?? 1500);
$targeting    = $data['targeting'] ?? [];
$ad           = $data['ad'] ?? [];
$adName       = $ad['name'] ?? 'Объявление';
$adTitle      = $ad['title'] ?? $adName;
$adDesc       = $ad['description'] ?? '';

$campaignData = [['name' => $campaignName, 'type' => 1, 'day_limit' => $dayLimit, 'all_limit' => $allLimit]];
$resCampaign = vkApi('ads.createCampaigns', [
    'account_id' => $accountId,
    'data'       => json_encode($campaignData, JSON_UNESCAPED_UNICODE),
], $accessToken, $VK_API_VERSION);

if (!empty($resCampaign['error'])) {
    htmlPage('Ошибка создания кампании', '<p>VK: ' . htmlspecialchars($resCampaign['error']) . '</p>');
}

$campaignId = $resCampaign['response'][0]['id'] ?? null;
if ($campaignId === null) {
    $err = $resCampaign['response'][0]['error_desc'] ?? 'Не удалось создать кампанию';
    htmlPage('Ошибка', '<p>' . htmlspecialchars($err) . '</p>');
}

$groupName = $adName;
$targetingJson = json_encode($targeting, JSON_UNESCAPED_UNICODE);
$adGroupsData = [[
    'name'        => mb_substr($groupName, 0, 100),
    'campaign_id' => $campaignId,
    'day_limit'   => $dayLimit,
    'bid'         => $bid,
    'targeting'   => $targetingJson,
]];
$resGroups = vkApi('ads.createAdGroups', [
    'account_id'  => $accountId,
    'campaign_id' => $campaignId,
    'data'        => json_encode($adGroupsData, JSON_UNESCAPED_UNICODE),
], $accessToken, $VK_API_VERSION);

if (!empty($resGroups['error'])) {
    htmlPage('Ошибка создания группы объявлений', '<p>VK: ' . htmlspecialchars($resGroups['error']) . '</p>');
}

$adGroupId = $resGroups['response'][0]['id'] ?? null;
if ($adGroupId === null) {
    $err = $resGroups['response'][0]['error_desc'] ?? 'Не удалось создать группу объявлений';
    htmlPage('Ошибка', '<p>' . htmlspecialchars($err) . '</p>');
}

$adsData = [[
    'campaign_id' => $campaignId,
    'ad_group_id' => $adGroupId,
    'name'        => mb_substr($adName, 0, 100),
    'link_url'    => $linkUrl,
    'title'       => mb_substr($adTitle, 0, 80),
    'description' => mb_substr($adDesc, 0, 800),
    'ad_format'   => 9,
]];
$resAds = vkApi('ads.createAds', [
    'account_id' => $accountId,
    'data'       => json_encode($adsData, JSON_UNESCAPED_UNICODE),
], $accessToken, $VK_API_VERSION);

if (!empty($resAds['error'])) {
    htmlPage('Ошибка создания объявления', '<p>VK: ' . htmlspecialchars($resAds['error']) . '</p>');
}

$adId = $resAds['response'][0]['id'] ?? null;
if ($adId === null) {
    $err = $resAds['response'][0]['error_desc'] ?? 'Не удалось создать объявление';
    htmlPage('Ошибка', '<p>' . htmlspecialchars($err) . '</p>');
}

htmlPage(
    'Кампания создана',
    '<p>Рекламная кампания создана в вашем кабинете ВКонтакте.</p>' .
    '<p><a href="https://vk.com/ads?act=office&amp;account_id=' . (int)$accountId . '">Открыть кабинет ВК Рекламы</a></p>'
);
