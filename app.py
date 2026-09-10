"""
Flask веб-приложение для анализа волн Эллиота
Оптимизировано для Render deployment
"""

from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
import logging
from binance_connector import BinanceConnector
from elliott_wave_analyzer import ElliottWaveAnalyzer
from fibonacci_calculator import FibonacciCalculator
import io
import base64
import matplotlib
matplotlib.use('Agg')  # Для веб-сервера
import matplotlib.pyplot as plt
from visualizer import WaveVisualizer
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Глобальные переменные для хранения данных
analysis_results = {}

# Конфигурация
app.config['JSON_SORT_KEYS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max


@app.route('/')
def index():
    """Главная страница"""
    return render_template('index.html')


@app.route('/api/health', methods=['GET'])
def health():
    """Проверка здоровья сервера"""
    return jsonify({'status': 'ok', 'message': 'Elliott Wave Analyzer is running'}), 200


@app.route('/api/analyze', methods=['POST'])
def analyze():
    """API для анализа волн Эллиота"""
    try:
        data = request.json
        symbol = data.get('symbol', 'ATOMUSDT')
        timeframe = data.get('timeframe', '1h')
        limit = int(data.get('limit', 800))
        
        logger.info(f"Начало анализа: {symbol} {timeframe}")
        
        # Подключение к Binance
        connector = BinanceConnector()
        df = connector.get_klines(symbol, timeframe, limit)
        
        if df is None:
            return jsonify({'error': 'Не удалось загрузить данные с Binance'}), 400
        
        # Анализ волн Эллиота
        analyzer = ElliottWaveAnalyzer(df)
        analyzer.find_extremes()
        analyzer.identify_waves()
        
        # Получить данные
        waves = analyzer.waves
        summary = analyzer.get_summary()
        extremes = analyzer.extremes
        
        # Расчёт Фибоначчи
        if len(waves) >= 2:
            last_wave = waves[-1]
            high = max(last_wave['start_price'], last_wave['end_price'])
            low = min(last_wave['start_price'], last_wave['end_price'])
            
            fib_calc = FibonacciCalculator(high, low)
            fib_levels = fib_calc.calculate_support_resistance()
        else:
            fib_levels = {}
        
        # Сохранение результатов
        analysis_results[timeframe] = {
            'df': df,
            'waves': waves,
            'fib_levels': fib_levels,
            'extremes': extremes
        }
        
        # Преобразование для JSON
        waves_json = []
        for wave in waves:
            waves_json.append({
                'wave': wave['wave'],
                'type': wave['type'],
                'start_price': float(wave['start_price']),
                'end_price': float(wave['end_price']),
                'start_date': wave['start_date'].isoformat(),
                'end_date': wave['end_date'].isoformat(),
                'movement': float(wave['movement']),
                'change_percent': float(wave['change_percent'])
            })
        
        fib_levels_json = {k: float(v) for k, v in fib_levels.items()}
        
        return jsonify({
            'success': True,
            'symbol': symbol,
            'timeframe': timeframe,
            'current_price': float(summary['current_price']),
            'total_waves': summary['total_waves'],
            'impulse_waves': summary['impulse_waves'],
            'corrective_waves': summary['corrective_waves'],
            'waves': waves_json,
            'fibonacci_levels': fib_levels_json,
            'data_points': len(df)
        })
    
    except Exception as e:
        logger.error(f"Ошибка анализа: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/chart/<timeframe>')
def get_chart(timeframe):
    """API для получения графика"""
    try:
        if timeframe not in analysis_results:
            return jsonify({'error': 'Данные не найдены'}), 404
        
        data = analysis_results[timeframe]
        df = data['df']
        waves = data['waves']
        fib_levels = data['fib_levels']
        
        # Создание графика
        visualizer = WaveVisualizer(df, figsize=(14, 7))
        fig, ax = visualizer.plot_waves_and_fibonacci(
            waves,
            fib_levels,
            title=f'Elliott Waves Analysis - {timeframe.upper()}'
        )
        
        # Сохранение в base64
        buffer = io.BytesIO()
        plt.savefig(buffer, format='png', dpi=80, bbox_inches='tight')
        buffer.seek(0)
        image_base64 = base64.b64encode(buffer.read()).decode()
        plt.close(fig)
        
        return jsonify({
            'success': True,
            'image': f'data:image/png;base64,{image_base64}'
        })
    
    except Exception as e:
        logger.error(f"Ошибка при создании графика: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/timeframes')
def get_timeframes():
    """API для получения доступных таймфреймов"""
    timeframes = ['1m', '5m', '15m', '1h', '4h', '1d']
    return jsonify({'timeframes': timeframes})


@app.route('/api/symbols')
def get_symbols():
    """API для получения доступных символов"""
    symbols = ['ATOMUSDT', 'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'ADAUSDT', 'DOGEUSDT']
    return jsonify({'symbols': symbols})


@app.route('/api/multi-analyze', methods=['POST'])
def multi_analyze():
    """API для анализа по нескольким таймфреймам"""
    try:
        data = request.json
        symbol = data.get('symbol', 'ATOMUSDT')
        timeframes = data.get('timeframes', ['1h', '4h', '1d'])
        limit = int(data.get('limit', 800))
        
        logger.info(f"Анализ нескольких таймфреймов: {symbol}")
        
        connector = BinanceConnector()
        results = {}
        
        for timeframe in timeframes:
            df = connector.get_klines(symbol, timeframe, limit)
            
            if df is None:
                logger.warning(f"Не удалось загрузить данные для {timeframe}")
                continue
            
            analyzer = ElliottWaveAnalyzer(df)
            analyzer.find_extremes()
            analyzer.identify_waves()
            
            summary = analyzer.get_summary()
            waves = analyzer.waves
            
            # Фибоначчи
            if len(waves) >= 2:
                last_wave = waves[-1]
                high = max(last_wave['start_price'], last_wave['end_price'])
                low = min(last_wave['start_price'], last_wave['end_price'])
                fib_calc = FibonacciCalculator(high, low)
                fib_levels = fib_calc.calculate_support_resistance()
            else:
                fib_levels = {}
            
            analysis_results[timeframe] = {
                'df': df,
                'waves': waves,
                'fib_levels': fib_levels,
                'extremes': analyzer.extremes
            }
            
            results[timeframe] = {
                'current_price': float(summary['current_price']),
                'total_waves': summary['total_waves'],
                'impulse_waves': summary['impulse_waves'],
                'corrective_waves': summary['corrective_waves'],
                'waves_count': len(waves),
                'fibonacci_levels': {k: float(v) for k, v in fib_levels.items()}
            }
        
        return jsonify({
            'success': True,
            'symbol': symbol,
            'results': results
        })
    
    except Exception as e:
        logger.error(f"Ошибка multi-analyze: {e}")
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
