/* The connection page.
 *
 * Everything on it comes from /api/v1/lead-engine/connect, which is
 * readable WITHOUT a token on purpose: the first thing someone needs to
 * know is whether the deployment is configured at all, and that question
 * should not itself require a credential.
 *
 * The token is never fetched, never displayed and never placed in the
 * snippets - they carry the environment variable name instead. A page
 * that prints a key is a page that leaks it into a screenshot.
 */
(function (global) {
  'use strict';

  function el(id) { return document.getElementById(id); }

  function set(id, text) {
    var node = el(id);
    if (node && node.textContent !== text) node.textContent = text;
  }

  function chip(id, ok, text) {
    var node = el(id);
    if (!node) return;
    node.textContent = text;
    node.className = 'le-chip ' + (ok ? 'ok' : 'bad');
  }

  var TOOLS = [
    'get_lead_engine_status', 'get_symbols', 'get_health',
    'get_market_snapshot', 'get_multi_tf_snapshot', 'get_orderbook_state',
    'get_trade_flow', 'get_derivatives_state', 'get_structure',
    'get_elliott_state', 'get_pressure', 'get_active_signal',
    'get_recent_signals', 'get_candles', 'get_indicators',
    'get_fibonacci_levels'
  ];

  function render(info) {
    var enabled = !!info.external_access_enabled;
    var keyed = !!info.api_key_configured;
    var ready = enabled && keyed;

    chip('cnState', ready, ready ? 'готово к подключению' : 'не настроено');
    set('cnEnabled', enabled ? 'включён' : 'выключен (' + info.flag_env + ')');
    set('cnKey', keyed ? 'да' : 'нет (' + info.api_key_env + ')');
    var rate = info.rate_limit || {};
    set('cnRate', rate.per_second + '/с, всплеск ' + rate.burst +
                  ' за ' + rate.burst_window_seconds + 'с');

    var hint = '';
    if (!enabled) {
      hint = 'Внешний доступ выключен. Задайте ' + info.flag_env +
             '=true в переменных окружения деплоя.';
    } else if (!keyed) {
      hint = 'Ключ не задан, поэтому API отвечает 503 на любой запрос. ' +
             'Сгенерируйте случайный токен и положите его в ' +
             info.api_key_env + ' в переменных окружения. Ключ нигде не ' +
             'показывается и не попадает в репозиторий.';
    } else {
      hint = 'Ключ задан. Он не отображается здесь намеренно — возьмите ' +
             'его из переменных окружения деплоя.';
    }
    set('cnHint', hint);

    set('cnMcpUrl', info.mcp_http_url);

    set('cnClaude', JSON.stringify({
      mcpServers: {
        'lead-engine': {
          command: 'python',
          args: ['-m', 'lead_engine_mcp.server'],
          env: {
            LEAD_ENGINE_API_URL: (info.rest_base || '').replace('/api/v1/lead-engine', ''),
            LEAD_ENGINE_API_KEY: '<ваш ключ из ' + info.api_key_env + '>'
          }
        }
      }
    }, null, 2));

    set('cnCurl',
        'curl -H "Authorization: Bearer $' + info.api_key_env + '" \\\n     ' +
        info.snapshot_example);
    set('cnStream',
        'curl -N -H "Authorization: Bearer $' + info.api_key_env + '" \\\n     "' +
        info.stream_example + '"');

    set('cnTools', TOOLS.join(' · '));
  }

  function start() {
    fetch('/api/v1/lead-engine/connect', { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function (e) {
        chip('cnState', false, 'недоступно');
        set('cnHint', 'Не удалось прочитать состояние доступа: ' +
                      (e && e.message ? e.message : e));
      });
  }

  document.addEventListener('DOMContentLoaded', start);
})(window);
