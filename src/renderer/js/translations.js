/**
 * Polígono AI Hub - Internationalization (i18n)
 * Supported languages: English (en), Español (es)
 */

const translations = {
    en: {
        // App Title
        app_title: 'AI AUDIO HUB',

        // Navigation Tabs
        tab_vocal_remover: 'Vocal Remover',
        tab_stem_splitter: 'Stem Splitter',
        tab_settings: 'Settings',

        // Mode Descriptions (Brand Subtitle)
        mode_vocal_remover_subtitle: 'Extract Vocals & Instrumental (2 Tracks)',
        mode_splitter_subtitle: 'Separate into 4 Stems (Vocals, Bass, Drums, Other)',
        mode_settings_subtitle: 'Configure AI models, processing quality, and hardware',

        // Control Panel - Add Files
        panel_add_files: 'Add Files',
        dropzone_text: 'Drop Files Here',
        dropzone_hint: 'or click to browse',

        // Control Panel - Output Settings
        panel_output_settings: 'Output Settings',
        settings_output_format: 'Output Format',

        // Output Formats
        format_wav: 'WAV (Lossless)',
        format_flac: 'FLAC',
        format_mp3: 'MP3',

        // Action Buttons
        button_start_queue: 'Start Queue',
        button_stop: 'Stop',
        button_clear: 'Clear',

        // Queue Panel
        queue_title: 'Job Queue',
        queue_stats: '{pending} pending · {processing} processing · {completed} completed',

        // Job States
        job_ready: 'Ready',
        job_waiting: 'Waiting...',
        job_initializing: 'Initializing...',
        job_processing_ai: 'Processing AI: {progress}%',
        job_completed: 'Completed in {time}s',
        job_error: 'Error: {message}',
        job_cancelled: 'Cancelled by user',

        // Job Actions
        job_action_open: 'Open folder',
        job_action_remove: 'Remove',

        // Empty State
        empty_queue: 'No files in queue',

        // Debug Log
        debug_log_title: 'Debug Log',

        // Settings Panel
        settings_title: 'Advanced Settings',
        settings_subtitle: 'Configure AI models, processing quality, and hardware preferences',

        // Settings - AI Model Card
        settings_ai_model_title: 'AI Model',
        settings_ai_model_label: 'Select Model',
        settings_ai_model_option_ft: 'Demucs v4 Fine-tuned (Best Quality)',
        settings_ai_model_option_standard: 'Demucs v4 Standard',
        settings_ai_model_option_mdx: 'MDX-Net Extra (Faster)',
        settings_ai_model_description: 'Fine-tuned model provides the best quality but requires more processing time.',

        // Settings - Quality Card
        settings_quality_title: 'Processing Quality',
        settings_quality_label: 'Quality Level',
        settings_quality_option_fast: 'Fast (1 shift)',
        settings_quality_option_hq: 'High Quality (2 shifts)',
        settings_quality_option_ultra: 'Ultra Quality (5 shifts)',
        settings_quality_description: 'Higher quality uses multiple processing passes for better separation.',

        // Settings - Device Card
        settings_device_title: 'Hardware Device',
        settings_device_label: 'Processing Device',
        settings_device_option_auto: 'Auto-detect (Recommended)',
        settings_device_option_cuda: 'GPU (CUDA)',
        settings_device_option_cpu: 'CPU Only',
        settings_device_description: 'GPU acceleration (CUDA) is significantly faster if available.',

        // Settings - Language Card
        settings_language_title: 'Language / Idioma',
        settings_language_label: 'Select Language',
        settings_language_option_en: '🇺🇸 English',
        settings_language_option_es: '🇪🇸 Español',
        settings_language_description: 'Choose your preferred language for the interface.',

        // Console Messages
        console_queue_initialized: 'Queue system initialized',
        console_adding_files: 'Adding {count} file(s) to queue...',
        console_added_file: 'Added: {name}',
        console_invalid_file: 'Invalid file: {name} - {reason}',
        console_queue_started: 'Queue processing started',
        console_queue_finished: 'Queue processing finished',
        console_processing_file: 'Processing: {name}',
        console_job_completed: 'Job {id} completed successfully',
        console_job_cancelled: 'Job {id} cancelled',
        console_job_error: 'Error processing {name}: {error}',
        console_stopping_queue: 'Stopping queue...',
        console_removed_jobs: 'Removed {count} completed job(s)',
        console_removed_job: 'Removed job: {name}',
        console_waiting_next: 'Waiting 1s before next job...',
        console_motor_started: 'Motor started for job {id}: {model} on {device}',
        console_mode_switched_vocal: 'Switched to Vocal Remover mode',
        console_mode_switched_splitter: 'Switched to Stem Splitter mode',
        console_settings_opened: 'Opened Settings panel',
        console_model_changed: 'AI Model changed to: {value}',
        console_quality_changed: 'Quality changed to: {value}',
        console_device_changed: 'Device changed to: {value}',
        console_language_changed: 'Language changed to: {value}',
        console_drag_drop_disabled: 'Drag & Drop is not currently supported',
        console_drag_drop_use_browse: 'Please use the "Browse" button to select multiple files',
    },

    es: {
        // App Title
        app_title: 'AI AUDIO HUB',

        // Navigation Tabs
        tab_vocal_remover: 'Separar Voces',
        tab_stem_splitter: 'Separador Stems',
        tab_settings: 'Configuración',

        // Mode Descriptions (Brand Subtitle)
        mode_vocal_remover_subtitle: 'Extraer Voces e Instrumental (2 Pistas)',
        mode_splitter_subtitle: 'Separar en 4 Stems (Voces, Bajo, Batería, Otros)',
        mode_settings_subtitle: 'Configurar modelos IA, calidad de procesamiento y hardware',

        // Control Panel - Add Files
        panel_add_files: 'Añadir Archivos',
        dropzone_text: 'Arrastra Archivos Aquí',
        dropzone_hint: 'o haz clic para explorar',

        // Control Panel - Output Settings
        panel_output_settings: 'Configuración de Salida',
        settings_output_format: 'Formato de Salida',

        // Output Formats
        format_wav: 'WAV (Sin pérdida)',
        format_flac: 'FLAC',
        format_mp3: 'MP3',

        // Action Buttons
        button_start_queue: 'Iniciar Cola',
        button_stop: 'Detener',
        button_clear: 'Limpiar',

        // Queue Panel
        queue_title: 'Cola de Trabajos',
        queue_stats: '{pending} pendientes · {processing} procesando · {completed} completados',

        // Job States
        job_ready: 'Listo',
        job_waiting: 'Esperando...',
        job_initializing: 'Inicializando...',
        job_processing_ai: 'Procesando IA: {progress}%',
        job_completed: 'Completado en {time}s',
        job_error: 'Error: {message}',
        job_cancelled: 'Cancelado por el usuario',

        // Job Actions
        job_action_open: 'Abrir carpeta',
        job_action_remove: 'Eliminar',

        // Empty State
        empty_queue: 'No hay archivos en la cola',

        // Debug Log
        debug_log_title: 'Registro de Depuración',

        // Settings Panel
        settings_title: 'Configuración Avanzada',
        settings_subtitle: 'Configurar modelos IA, calidad de procesamiento y preferencias de hardware',

        // Settings - AI Model Card
        settings_ai_model_title: 'Modelo IA',
        settings_ai_model_label: 'Seleccionar Modelo',
        settings_ai_model_option_ft: 'Demucs v4 Refinado (Mejor Calidad)',
        settings_ai_model_option_standard: 'Demucs v4 Estándar',
        settings_ai_model_option_mdx: 'MDX-Net Extra (Más Rápido)',
        settings_ai_model_description: 'El modelo refinado proporciona la mejor calidad pero requiere más tiempo de procesamiento.',

        // Settings - Quality Card
        settings_quality_title: 'Calidad de Procesamiento',
        settings_quality_label: 'Nivel de Calidad',
        settings_quality_option_fast: 'Rápido (1 turno)',
        settings_quality_option_hq: 'Alta Calidad (2 turnos)',
        settings_quality_option_ultra: 'Calidad Ultra (5 turnos)',
        settings_quality_description: 'Mayor calidad utiliza múltiples pases de procesamiento para mejor separación.',

        // Settings - Device Card
        settings_device_title: 'Dispositivo de Hardware',
        settings_device_label: 'Dispositivo de Procesamiento',
        settings_device_option_auto: 'Detectar automáticamente (Recomendado)',
        settings_device_option_cuda: 'GPU (CUDA)',
        settings_device_option_cpu: 'Solo CPU',
        settings_device_description: 'La aceleración GPU (CUDA) es significativamente más rápida si está disponible.',

        // Settings - Language Card
        settings_language_title: 'Language / Idioma',
        settings_language_label: 'Seleccionar Idioma',
        settings_language_option_en: '🇺🇸 English',
        settings_language_option_es: '🇪🇸 Español',
        settings_language_description: 'Elige tu idioma preferido para la interfaz.',

        // Console Messages
        console_queue_initialized: 'Sistema de cola inicializado',
        console_adding_files: 'Añadiendo {count} archivo(s) a la cola...',
        console_added_file: 'Añadido: {name}',
        console_invalid_file: 'Archivo inválido: {name} - {reason}',
        console_queue_started: 'Procesamiento de cola iniciado',
        console_queue_finished: 'Procesamiento de cola finalizado',
        console_processing_file: 'Procesando: {name}',
        console_job_completed: 'Trabajo {id} completado exitosamente',
        console_job_cancelled: 'Trabajo {id} cancelado',
        console_job_error: 'Error procesando {name}: {error}',
        console_stopping_queue: 'Deteniendo cola...',
        console_removed_jobs: 'Eliminados {count} trabajo(s) completado(s)',
        console_removed_job: 'Trabajo eliminado: {name}',
        console_waiting_next: 'Esperando 1s antes del siguiente trabajo...',
        console_motor_started: 'Motor iniciado para trabajo {id}: {model} en {device}',
        console_mode_switched_vocal: 'Cambiado a modo Separar Voces',
        console_mode_switched_splitter: 'Cambiado a modo Separador Stems',
        console_settings_opened: 'Panel de Configuración abierto',
        console_model_changed: 'Modelo IA cambiado a: {value}',
        console_quality_changed: 'Calidad cambiada a: {value}',
        console_device_changed: 'Dispositivo cambiado a: {value}',
        console_language_changed: 'Idioma cambiado a: {value}',
        console_drag_drop_disabled: 'Arrastrar y soltar no está soportado actualmente',
        console_drag_drop_use_browse: 'Por favor usa el botón "Explorar" para seleccionar múltiples archivos',
    }
};

// Helper function to replace placeholders like {name}, {count}, etc.
function interpolate(text, values) {
    if (!values) return text;
    return text.replace(/\{(\w+)\}/g, (match, key) => {
        return values.hasOwnProperty(key) ? values[key] : match;
    });
}

// Export for use in renderer
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { translations, interpolate };
}
