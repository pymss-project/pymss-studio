//! Minimal VST 2.4 ABI, checked against RustAudio/vst-rs src/{api,host,plugin,editor}.rs.
//! https://github.com/RustAudio/vst-rs (MIT). No Steinberg SDK headers are distributed.
//!
//! All pointers here originate from native code in the isolated helper. Callback data is
//! preallocated and atomic; neither processing nor host callbacks take a mutex or allocate.
use crate::Vst2Descriptor;
use libloading::Library;
use soundcraft_dsp::{
    Category, ParamInfo, PluginEditor, PluginInfo, PluginStateReader, Taper, Transport, Unit,
};
use soundcraft_vst3_host::NativeEditorWindow;
use std::cell::{Cell, UnsafeCell};
use std::collections::{HashMap, HashSet};
use std::ffi::{c_char, c_void};
use std::path::{Path, PathBuf};
use std::ptr::{self, NonNull};
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU32, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock, Weak};
use std::thread::ThreadId;

/// Main input and output pins: a mono or stereo pair.
pub(crate) const MAIN_PINS: usize = 2;
/// Input pins accepted on the single VST2 input bus, including sidechain pins.
pub(crate) const MAX_INPUT_PINS: usize = 64;

const MAX_PARAMS: usize = 4096;
const MAX_SHELL_CHILDREN: usize = 4096;
const MAX_STATE: usize = 32 * 1024 * 1024;
const NONE: u64 = u64::MAX;
const MAGIC: i32 = 0x5673_7450;
const HAS_EDITOR: i32 = 1;
const CAN_REPLACING: i32 = 1 << 4;
const PROGRAM_CHUNKS: i32 = 1 << 5;
const IS_SYNTH: i32 = 1 << 8;

// VST effect dispatcher opcodes. Numeric values are ABI constants, not SDK definitions.
const OPEN: i32 = 0;
const CLOSE: i32 = 1;
const PARAM_LABEL: i32 = 6;
const PARAM_DISPLAY: i32 = 7;
const PARAM_NAME: i32 = 8;
const SAMPLE_RATE: i32 = 10;
const BLOCK_SIZE: i32 = 11;
const MAINS: i32 = 12;
const EDIT_RECT: i32 = 13;
const EDIT_OPEN: i32 = 14;
const EDIT_CLOSE: i32 = 15;
const EDIT_IDLE: i32 = 19;
const GET_CHUNK: i32 = 23;
const SET_CHUNK: i32 = 24;
const CATEGORY: i32 = 35;
const EFFECT_NAME: i32 = 45;
const VENDOR: i32 = 47;
const PRODUCT: i32 = 48;
const TAIL: i32 = 52;
const API_VERSION: i32 = 58;
const SHELL_NEXT: i32 = 70;
const START: i32 = 71;
const STOP: i32 = 72;
const PRECISION: i32 = 77;
const NATIVE_EDITOR_SUPPORTED: bool = cfg!(any(windows, target_os = "macos", target_os = "linux"));

type HostCallback = unsafe extern "C" fn(*mut Effect, i32, i32, isize, *mut c_void, f32) -> isize;
type Dispatcher = unsafe extern "C" fn(*mut Effect, i32, i32, isize, *mut c_void, f32) -> isize;
type Process = unsafe extern "C" fn(*mut Effect, *const *const f32, *mut *mut f32, i32);
type SetParameter = unsafe extern "C" fn(*mut Effect, i32, f32);
type GetParameter = unsafe extern "C" fn(*mut Effect, i32) -> f32;
type Entry = unsafe extern "C" fn(HostCallback) -> *mut Effect;

#[repr(C)]
struct Effect {
    magic: i32,
    dispatcher: Option<Dispatcher>,
    legacy_process: Option<Process>,
    set_parameter: Option<SetParameter>,
    get_parameter: Option<GetParameter>,
    programs: i32,
    parameters: i32,
    inputs: i32,
    outputs: i32,
    flags: i32,
    reserved1: isize,
    reserved2: isize,
    initial_delay: i32,
    real_qualities: i32,
    off_qualities: i32,
    io_ratio: f32,
    object: *mut c_void,
    user: *mut c_void,
    unique_id: i32,
    version: i32,
    process_replacing: Option<Process>,
    process_replacing_f64: Option<unsafe extern "C" fn()>,
    future: [u8; 56],
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct TimeInfo {
    sample_pos: f64,
    sample_rate: f64,
    nanoseconds: f64,
    ppq_pos: f64,
    tempo: f64,
    bar_start: f64,
    cycle_start: f64,
    cycle_end: f64,
    numerator: i32,
    denominator: i32,
    smpte_offset: i32,
    smpte_rate: i32,
    next_clock: i32,
    flags: i32,
}

thread_local! {
    // Callbacks can arrive before AEffect is returned, or pass null during a later host call.
    // Context is scoped to the current thread's native call, never a process-wide selector.
    static LOADING: Cell<*const HostContext> = const { Cell::new(ptr::null()) };
    static PROCESS_LEVEL: Cell<isize> = const { Cell::new(1) };
    // VST time-info pointers are borrowed until the next host call on this same thread.
    static TIME: UnsafeCell<TimeInfo> = const { UnsafeCell::new(TimeInfo {
        sample_pos: 0.0, sample_rate: 48_000.0, nanoseconds: 0.0, ppq_pos: 0.0,
        tempo: 120.0, bar_start: 0.0, cycle_start: 0.0, cycle_end: 0.0,
        numerator: 4, denominator: 4, smpte_offset: 0, smpte_rate: 0,
        next_clock: 0, flags: 0,
    }) };
}

struct LoadingGuard(*const HostContext);
impl LoadingGuard {
    fn new(host: &HostContext) -> Self {
        Self(LOADING.with(|slot| slot.replace(host)))
    }
}
impl Drop for LoadingGuard {
    fn drop(&mut self) {
        LOADING.with(|slot| slot.set(self.0));
    }
}

struct ProcessLevelGuard(isize);
impl ProcessLevelGuard {
    fn processing(offline: bool) -> Self {
        Self::enter(if offline { 4 } else { 2 })
    }
    fn user() -> Self {
        Self::enter(1)
    }
    fn enter(level: isize) -> Self {
        Self(PROCESS_LEVEL.with(|current| current.replace(level)))
    }
}
impl Drop for ProcessLevelGuard {
    fn drop(&mut self) {
        PROCESS_LEVEL.with(|level| level.set(self.0));
    }
}

struct HostContext {
    requested_id: i32,
    count: AtomicUsize,
    values: Box<[AtomicU32]>,
    pending: Box<[AtomicU64]>,
    edits: Box<[AtomicU64]>,
    refresh: AtomicBool,
    io_changed: AtomicBool,
    invalid_layout: AtomicBool,
    configured_inputs: AtomicUsize,
    configured_outputs: AtomicUsize,
    latency: AtomicUsize,
    width: AtomicU32,
    height: AtomicU32,
    sample_rate: AtomicU64,
    block_size: AtomicUsize,
    active: AtomicBool,
    samples: AtomicI64,
    expected_samples: AtomicI64,
    tempo: AtomicU64,
    quarters: AtomicU64,
    bar_start: AtomicU64,
    numerator: AtomicU32,
    denominator: AtomicU32,
    transport_flags: AtomicU32,
}

impl HostContext {
    fn new() -> Self {
        Self {
            requested_id: 0,
            count: AtomicUsize::new(MAX_PARAMS),
            values: (0..MAX_PARAMS).map(|_| AtomicU32::new(0)).collect(),
            pending: (0..MAX_PARAMS).map(|_| AtomicU64::new(NONE)).collect(),
            edits: (0..MAX_PARAMS).map(|_| AtomicU64::new(NONE)).collect(),
            refresh: AtomicBool::new(false),
            io_changed: AtomicBool::new(false),
            invalid_layout: AtomicBool::new(false),
            configured_inputs: AtomicUsize::new(0),
            configured_outputs: AtomicUsize::new(0),
            latency: AtomicUsize::new(0),
            width: AtomicU32::new(0),
            height: AtomicU32::new(0),
            sample_rate: AtomicU64::new(48_000f64.to_bits()),
            block_size: AtomicUsize::new(256),
            active: AtomicBool::new(false),
            samples: AtomicI64::new(0),
            expected_samples: AtomicI64::new(i64::MIN),
            tempo: AtomicU64::new(120f64.to_bits()),
            quarters: AtomicU64::new(0),
            bar_start: AtomicU64::new(0),
            numerator: AtomicU32::new(4),
            denominator: AtomicU32::new(4),
            transport_flags: AtomicU32::new(0),
        }
    }

    fn index(&self, id: &str) -> Option<usize> {
        // A canonical decimal parameter id prevents aliases such as +1 or 0001.
        if id.is_empty()
            || id.len() > 4
            || (id.len() > 1 && id.starts_with('0'))
            || !id.bytes().all(|byte| byte.is_ascii_digit())
        {
            return None;
        }
        let index = id.parse::<usize>().ok()?;
        (index < self.count.load(Ordering::Relaxed)).then_some(index)
    }

    fn update_transport(&self, transport: &Transport) {
        let tempo = if transport.tempo.is_finite() && transport.tempo > 0.0 {
            transport.tempo
        } else {
            120.0
        };
        let quarters = if transport.quarters.is_finite() {
            transport.quarters
        } else {
            0.0
        };
        let bar_start = if transport.bar_start.is_finite() {
            transport.bar_start
        } else {
            0.0
        };
        self.samples.store(transport.samples, Ordering::Relaxed);
        self.tempo.store(tempo.to_bits(), Ordering::Relaxed);
        self.quarters.store(quarters.to_bits(), Ordering::Relaxed);
        self.bar_start.store(bar_start.to_bits(), Ordering::Relaxed);
        self.numerator
            .store(transport.numerator.clamp(1, 128), Ordering::Relaxed);
        self.denominator
            .store(transport.denominator.clamp(1, 128), Ordering::Relaxed);
        let previous = self.transport_flags.load(Ordering::Relaxed) & !1;
        let flags = u32::from(transport.playing) * 2 | u32::from(transport.recording) * 8;
        let moved = self.expected_samples.load(Ordering::Relaxed) != transport.samples;
        self.transport_flags.store(
            flags | u32::from(previous != flags || moved),
            Ordering::Relaxed,
        );
    }
}

unsafe extern "C" fn callback(
    effect: *mut Effect,
    opcode: i32,
    index: i32,
    value: isize,
    data: *mut c_void,
    opt: f32,
) -> isize {
    if opcode == 1 {
        return 2400;
    }
    // SAFETY: a plugin passes its live AEffect, or null while its entry point executes.
    let pointer = if effect.is_null() {
        ptr::null()
    } else {
        unsafe { (*effect).reserved1 as *const HostContext }
    };
    let pointer = if pointer.is_null() {
        LOADING.with(Cell::get)
    } else {
        pointer
    };
    // SAFETY: HostContext is pinned in Shared/Instance until effClose has returned.
    let Some(host) = (unsafe { pointer.as_ref() }) else {
        return 0;
    };
    match opcode {
        2 => host.requested_id as isize,
        0 if index >= 0
            && (index as usize) < host.count.load(Ordering::Relaxed)
            && opt.is_finite() =>
        {
            let index = index as usize;
            let normalized = opt.clamp(0.0, 1.0);
            host.values[index].store(normalized.to_bits(), Ordering::Relaxed);
            host.edits[index].store(u64::from(normalized.to_bits()), Ordering::Release);
            0
        }
        7 => TIME.with(|time| {
            let info = TimeInfo {
                sample_pos: host.samples.load(Ordering::Relaxed) as f64,
                sample_rate: f64::from_bits(host.sample_rate.load(Ordering::Relaxed)),
                ppq_pos: f64::from_bits(host.quarters.load(Ordering::Relaxed)),
                tempo: f64::from_bits(host.tempo.load(Ordering::Relaxed)),
                bar_start: f64::from_bits(host.bar_start.load(Ordering::Relaxed)),
                numerator: host.numerator.load(Ordering::Relaxed) as i32,
                denominator: host.denominator.load(Ordering::Relaxed) as i32,
                flags: host.transport_flags.load(Ordering::Relaxed) as i32
                    | (1 << 9)
                    | (1 << 10)
                    | (1 << 11)
                    | (1 << 13),
                ..TimeInfo::default()
            };
            // SAFETY: this cell belongs to the calling thread and is written before borrowing.
            unsafe {
                time.get().write(info);
            }
            time.get() as isize
        }),
        13 => {
            host.io_changed.store(true, Ordering::Release);
            // A latency update can be accepted, but the fixed audio bus/parameter tables
            // cannot be changed under the current processReplacing call.
            let valid = unsafe { effect.as_ref() }.is_some_and(|effect| {
                let inputs = host.configured_inputs.load(Ordering::Relaxed);
                let outputs = host.configured_outputs.load(Ordering::Relaxed);
                (inputs == 0
                    || (effect.inputs as usize == inputs && effect.outputs as usize == outputs))
                    && effect.parameters as usize == host.count.load(Ordering::Relaxed)
            });
            if !valid {
                host.invalid_layout.store(true, Ordering::Release);
            }
            isize::from(valid)
        }
        15 if (1..=16_384).contains(&index) && (1..=16_384).contains(&value) => {
            host.width.store(index as u32, Ordering::Relaxed);
            host.height.store(value as u32, Ordering::Release);
            // Resize remains a queued UI-thread operation, including X11/XWayland hosts.
            isize::from(NATIVE_EDITOR_SUPPORTED)
        }
        16 => f64::from_bits(host.sample_rate.load(Ordering::Relaxed)) as isize,
        17 => host.block_size.load(Ordering::Relaxed) as isize,
        22 => 1, // replacing mode
        23 => PROCESS_LEVEL.with(Cell::get),
        24 => 1, // automation off; this host does not claim write automation
        32 => {
            unsafe { copy_string(data, b"Pymss Studio", 64) };
            1
        }
        33 => {
            unsafe { copy_string(data, b"Pymss Studio", 64) };
            1
        }
        34 => 1,
        37 => {
            // SAFETY: canDo's argument is an SDK-owned NUL-terminated capability string.
            let capability = unsafe { bounded_c_string(data.cast(), 64) };
            match capability {
                b"sendVstTimeInfo" | b"startStopProcess" | b"supportShell" | b"shellCategory" => 1,
                b"sizeWindow" if NATIVE_EDITOR_SUPPORTED => 1,
                b"sendVstEvents"
                | b"sendVstMidiEvent"
                | b"receiveVstEvents"
                | b"receiveVstMidiEvent" => -1,
                _ => 0,
            }
        }
        38 => 1, // English
        42 => {
            host.refresh.store(true, Ordering::Release);
            1
        }
        43 | 44 if index >= 0 && (index as usize) < host.count.load(Ordering::Relaxed) => 1,
        _ => 0,
    }
}

unsafe fn bounded_c_string<'a>(pointer: *const c_char, limit: usize) -> &'a [u8] {
    if pointer.is_null() {
        return &[];
    }
    let mut length = 0;
    // SAFETY: native caller promises a C string; never scan beyond the ABI limit.
    while length < limit && unsafe { pointer.add(length).read() } != 0 {
        length += 1;
    }
    unsafe { std::slice::from_raw_parts(pointer.cast(), length) }
}

unsafe fn copy_string(destination: *mut c_void, text: &[u8], capacity: usize) {
    if destination.is_null() || capacity == 0 {
        return;
    }
    let length = text.len().min(capacity - 1);
    // SAFETY: the ABI supplies a writable buffer of the corresponding string limit.
    unsafe {
        ptr::copy_nonoverlapping(text.as_ptr(), destination.cast::<u8>(), length);
        destination.cast::<u8>().add(length).write(0);
    }
}

unsafe fn dispatch(
    effect: NonNull<Effect>,
    opcode: i32,
    index: i32,
    value: isize,
    data: *mut c_void,
    opt: f32,
) -> isize {
    // SAFETY: the effect remains live and its dispatcher was checked before constructing Instance.
    let dispatcher = unsafe { effect.as_ref().dispatcher };
    let host = unsafe { (effect.as_ref().reserved1 as *const HostContext).as_ref() };
    let _context = host.map(LoadingGuard::new);
    let _level = ProcessLevelGuard::user();
    dispatcher.map_or(0, |dispatcher| unsafe {
        dispatcher(effect.as_ptr(), opcode, index, value, data, opt)
    })
}

unsafe fn read_text(effect: NonNull<Effect>, opcode: i32, index: i32, capacity: usize) -> String {
    let mut data = [0u8; 256];
    // SAFETY: VST name/vendor/parameter buffers are <= 64 bytes, and our storage is larger.
    unsafe {
        dispatch(effect, opcode, index, 0, data.as_mut_ptr().cast(), 0.0);
    }
    let length = data
        .iter()
        .take(capacity.min(data.len()))
        .position(|byte| *byte == 0)
        .unwrap_or(capacity.min(data.len()));
    String::from_utf8_lossy(&data[..length])
        .chars()
        .filter(|character| !character.is_control())
        .collect::<String>()
        .trim()
        .to_owned()
}

#[repr(C)]
#[derive(Clone, Copy)]
struct EditorRect {
    top: i16,
    left: i16,
    bottom: i16,
    right: i16,
}

unsafe fn editor_dimensions(effect: NonNull<Effect>) -> Option<(u32, u32)> {
    let mut rectangle: *mut EditorRect = ptr::null_mut();
    // SAFETY: effect is live; the plugin returns a borrowed rectangle valid until its next call.
    unsafe {
        dispatch(
            effect,
            EDIT_RECT,
            0,
            0,
            (&mut rectangle as *mut *mut EditorRect).cast(),
            0.0,
        );
    }
    let rectangle = unsafe { rectangle.as_ref() }?;
    let width = i32::from(rectangle.right) - i32::from(rectangle.left);
    let height = i32::from(rectangle.bottom) - i32::from(rectangle.top);
    ((1..=16_384).contains(&width) && (1..=16_384).contains(&height))
        .then_some((width as u32, height as u32))
}

unsafe fn attach_editor(
    effect: NonNull<Effect>,
    parent: *mut c_void,
    is_x11: bool,
) -> Result<(), String> {
    if parent.is_null() {
        return Err("The VST2 editor parent is unavailable".into());
    }
    // Linux's parent is the numeric X11 Window ID, never Display* or an address of an XID.
    // X11 resource IDs occupy 32 protocol bits even when Xlib Window is an unsigned long.
    if is_x11 && parent as usize > u32::MAX as usize {
        return Err("The VST2 X11 parent window identifier is invalid".into());
    }
    // The modern JUCE host and JUCE/DPF plugin wrappers use effEditOpen.ptr = XID, value = 0.
    // Older Ardour-compatible plugins used value = Display* plus _XEventProc; that private
    // event-queue extension is not hosted. Modern plugins own their toolkit/display connection.
    // effEditOpen's return value is unspecified and is zero in conforming implementations.
    unsafe {
        dispatch(effect, EDIT_OPEN, 0, 0, parent, 0.0);
    }
    Ok(())
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

#[derive(Default)]
struct UiState {
    effect: usize,
    window: Option<NativeEditorWindow>,
    editor_attached: bool,
    dimensions: Option<(u32, u32)>,
    reported_dimensions: Option<(u32, u32)>,
}

impl UiState {
    fn observe_dimensions(
        &mut self,
        requested: Option<(u32, u32)>,
        reported: Option<(u32, u32)>,
    ) -> Option<(u32, u32)> {
        let changed = reported.filter(|dimensions| Some(*dimensions) != self.reported_dimensions);
        self.reported_dimensions = reported.or(self.reported_dimensions);
        let dimensions = requested.or(changed)?;
        if self.dimensions == Some(dimensions) {
            return None;
        }
        self.dimensions = Some(dimensions);
        Some(dimensions)
    }
}

pub(crate) struct Shared {
    host: HostContext,
    ui: Mutex<UiState>,
    owner: ThreadId,
    // Kept until effClose and every UI lease have finished. Existing editor handles use Weak.
    _library: Option<Arc<Library>>,
    binary: PathBuf,
    unique_id: i32,
    version: i32,
    flags: i32,
    retired: AtomicBool,
}

impl Shared {
    pub(crate) fn queue(&self, id: &str, value: f32) -> bool {
        if self.retired.load(Ordering::Acquire) {
            return false;
        }
        let Some(index) = self.host.index(id) else {
            return false;
        };
        let normalized = if value.is_nan() {
            0.0
        } else {
            value.clamp(0.0, 1.0)
        };
        self.host.values[index].store(normalized.to_bits(), Ordering::Relaxed);
        self.host.pending[index].store(u64::from(normalized.to_bits()), Ordering::Release);
        true
    }

    pub(crate) fn value(&self, id: &str) -> Option<f32> {
        if self.retired.load(Ordering::Acquire) {
            return None;
        }
        let index = self.host.index(id)?;
        Some(f32::from_bits(
            self.host.values[index].load(Ordering::Relaxed),
        ))
    }

    fn is_owner(&self) -> bool {
        self.owner == std::thread::current().id()
    }

    fn with_effect<R>(&self, operation: impl FnOnce(NonNull<Effect>) -> R) -> Option<R> {
        if !self.is_owner() || self.retired.load(Ordering::Acquire) {
            return None;
        }
        let state = lock(&self.ui);
        let effect = NonNull::new(state.effect as *mut Effect)?;
        let _level = ProcessLevelGuard::user();
        Some(operation(effect))
    }

    fn read_values(&self, effect: NonNull<Effect>, record_edits: bool) {
        let _context = LoadingGuard::new(&self.host);
        let _level = ProcessLevelGuard::user();
        // SAFETY: effect is protected by the UI lease or exclusively owned by Instance.
        let Some(get) = (unsafe { effect.as_ref().get_parameter }) else {
            return;
        };
        for index in 0..self.host.count.load(Ordering::Relaxed) {
            let value = unsafe { get(effect.as_ptr(), index as i32) };
            if !value.is_finite() {
                continue;
            }
            let value = value.clamp(0.0, 1.0).to_bits();
            let old = self.host.values[index].swap(value, Ordering::Relaxed);
            if record_edits && old != value {
                self.host.edits[index].store(u64::from(value), Ordering::Release);
            }
        }
    }

    fn flush(&self, effect: NonNull<Effect>) {
        let _context = LoadingGuard::new(&self.host);
        // SAFETY: native setParameter may run on the processing thread per VST2's contract.
        let Some(set) = (unsafe { effect.as_ref().set_parameter }) else {
            return;
        };
        for index in 0..self.host.count.load(Ordering::Relaxed) {
            let bits = self.host.pending[index].swap(NONE, Ordering::Acquire);
            if bits != NONE {
                unsafe {
                    set(effect.as_ptr(), index as i32, f32::from_bits(bits as u32));
                }
            }
        }
    }

    fn close_editor_locked(&self, state: &mut UiState) {
        if state.editor_attached {
            if let Some(effect) = NonNull::new(state.effect as *mut Effect) {
                unsafe {
                    dispatch(effect, EDIT_CLOSE, 0, 0, ptr::null_mut(), 0.0);
                }
            }
            state.editor_attached = false;
        }
        state.dimensions = None;
        state.reported_dimensions = None;
        self.host.width.store(0, Ordering::Relaxed);
        self.host.height.store(0, Ordering::Release);
        if let Some(window) = state.window.take() {
            window.close();
        }
    }

    fn close_now(&self) {
        if !self.is_owner() {
            return;
        }
        let mut state = lock(&self.ui);
        self.close_editor_locked(&mut state);
        if let Some(effect) = NonNull::new(std::mem::take(&mut state.effect) as *mut Effect) {
            unsafe {
                if self.host.active.swap(false, Ordering::Relaxed) {
                    dispatch(effect, STOP, 0, 0, ptr::null_mut(), 0.0);
                    dispatch(effect, MAINS, 0, 0, ptr::null_mut(), 0.0);
                }
                // AEffect and its reserved1 host context remain valid throughout effClose.
                dispatch(effect, CLOSE, 0, 0, ptr::null_mut(), 0.0);
            }
        }
    }

    fn open(&self, name: &str) -> Result<(), String> {
        if !self.is_owner() || !soundcraft_vst3_host::on_ui_thread() {
            return Err("VST2 editors must be opened on their owning UI thread".into());
        }
        if self.retired.load(Ordering::Acquire) {
            return Err("The VST2 instance is closed".into());
        }
        if self.flags & HAS_EDITOR == 0 {
            return Err("This VST2 plugin has no native editor".into());
        }
        if !NATIVE_EDITOR_SUPPORTED {
            return Err("VST2 native editors are not supported on this platform; use the parameter controls".into());
        }
        let mut state = lock(&self.ui);
        if let Some(window) = state.window.as_ref() {
            if window.is_visible() {
                #[cfg(windows)]
                window.update_owner()?;
                window.raise();
                return Ok(());
            }
        }
        self.close_editor_locked(&mut state);
        let effect =
            NonNull::new(state.effect as *mut Effect).ok_or("The VST2 instance is closed")?;
        #[cfg(target_os = "macos")]
        // Cockos' documented Cocoa negotiation switches older dual-view plugins to NSView.
        // Native 64-bit macOS plugins otherwise already use NSView; Carbon requires 32-bit.
        // SAFETY: the effect is live and the capability name is a static C string.
        unsafe {
            dispatch(
                effect,
                51,
                0,
                0,
                c"hasCockosViewAsConfig".as_ptr().cast_mut().cast(),
                0.0,
            );
        }
        // Some Linux plugins cannot report their rectangle until effEditOpen. Create a bounded
        // initial container and query again after attachment, without sleeping on the UI thread.
        let (width, height) = unsafe { editor_dimensions(effect) }.unwrap_or((400, 300));
        let window = NativeEditorWindow::new(name, f64::from(width), f64::from(height))?;
        // NSView on 64-bit macOS, HWND on Windows, numeric XID on Linux/XWayland.
        if let Err(error) =
            unsafe { attach_editor(effect, window.parent(), cfg!(target_os = "linux")) }
        {
            window.close();
            return Err(error);
        }
        state.editor_attached = true;
        state.dimensions = Some((width, height));
        let reported = unsafe { editor_dimensions(effect) };
        if let Some((width, height)) = reported {
            window.resize(f64::from(width), f64::from(height));
            state.dimensions = Some((width, height));
        }
        state.reported_dimensions = reported;
        window.raise();
        state.window = Some(window);
        Ok(())
    }

    fn close_editor(&self) {
        if self.is_owner() {
            self.close_editor_locked(&mut lock(&self.ui));
        }
    }

    fn idle(&self) -> Vec<(String, f32)> {
        if !self.is_owner() || self.retired.load(Ordering::Acquire) {
            return Vec::new();
        }
        let mut state = lock(&self.ui);
        let Some(effect) = NonNull::new(state.effect as *mut Effect) else {
            return Vec::new();
        };
        if state.editor_attached {
            if state
                .window
                .as_ref()
                .is_some_and(|window| !window.is_visible())
            {
                self.close_editor_locked(&mut state);
            } else {
                unsafe {
                    dispatch(effect, EDIT_IDLE, 0, 0, ptr::null_mut(), 0.0);
                }
                let height = self.host.height.swap(0, Ordering::Acquire);
                let requested = if height > 0 {
                    Some((self.host.width.load(Ordering::Relaxed), height))
                } else {
                    None
                };
                // Honor newly reported rectangles without letting an unchanged/stale rectangle
                // undo a successful audioMasterSizeWindow request on the following UI tick.
                let reported = unsafe { editor_dimensions(effect) };
                if let Some((width, height)) = state.observe_dimensions(requested, reported) {
                    if let Some(window) = state.window.as_ref() {
                        window.resize(f64::from(width), f64::from(height));
                    }
                }
            }
        }
        if self.host.refresh.swap(false, Ordering::Acquire) {
            self.read_values(effect, true);
        }
        let mut edits = Vec::new();
        for index in 0..self.host.count.load(Ordering::Relaxed) {
            let bits = self.host.edits[index].swap(NONE, Ordering::Acquire);
            if bits != NONE {
                edits.push((index.to_string(), f32::from_bits(bits as u32)));
            }
        }
        edits
    }

    fn save(&self) -> Option<Vec<u8>> {
        self.with_effect(|effect| {
            // The caller's graph snapshot barrier must suspend the processing worker first.
            self.flush(effect);
            self.read_values(effect, false);
            let mut chunk: *mut c_void = ptr::null_mut();
            let mut data = Vec::new();
            let kind;
            if self.flags & PROGRAM_CHUNKS != 0 {
                let length = unsafe {
                    dispatch(
                        effect,
                        GET_CHUNK,
                        0,
                        0,
                        (&mut chunk as *mut *mut c_void).cast(),
                        0.0,
                    )
                };
                if length <= 0 || length as usize > MAX_STATE - STATE_HEADER || chunk.is_null() {
                    return None;
                }
                // The plugin owns the returned bytes until the next dispatcher call.
                data.extend_from_slice(unsafe {
                    std::slice::from_raw_parts(chunk.cast::<u8>(), length as usize)
                });
                kind = 1;
            } else {
                for value in self
                    .host
                    .values
                    .iter()
                    .take(self.host.count.load(Ordering::Relaxed))
                {
                    data.extend_from_slice(&value.load(Ordering::Relaxed).to_le_bytes());
                }
                kind = 2;
            }
            Some(encode_state(
                self.unique_id,
                self.version,
                kind,
                self.host.count.load(Ordering::Relaxed),
                &data,
            ))
        })
        .flatten()
    }
}

static RETIRED: OnceLock<Mutex<Vec<Arc<Shared>>>> = OnceLock::new();

pub(crate) fn pump_ui() {
    let Some(queue) = RETIRED.get() else {
        return;
    };
    let mut ready = Vec::new();
    {
        let mut queue = lock(queue);
        let mut index = 0;
        while index < queue.len() {
            if queue[index].is_owner() {
                ready.push(queue.swap_remove(index));
            } else {
                index += 1;
            }
        }
    }
    for shared in ready {
        shared.close_now();
    }
}

pub(crate) struct Instance {
    effect: NonNull<Effect>,
    shared: Arc<Shared>,
    inputs: usize,
    outputs: usize,
    active: bool,
    offline: bool,
}

// AEffect's processor belongs to this unique owner; UI operations use the separate lifecycle
// lease. VST2 explicitly permits editor/parameter calls alongside processReplacing.
unsafe impl Send for Instance {}

impl Instance {
    pub(crate) fn load(path: &Path) -> Result<Self, String> {
        Self::load_selected(path, 0)
    }

    pub(crate) fn load_selected(path: &Path, requested_id: i32) -> Result<Self, String> {
        let binary = binary_path(path)?;
        let library =
            Arc::new(unsafe { Library::new(&binary) }.map_err(|error| {
                format!("Cannot load VST2 binary {}: {error}", binary.display())
            })?);
        let entry = *unsafe {
            library
                .get::<Entry>(b"VSTPluginMain\0")
                .or_else(|_| library.get::<Entry>(b"main\0"))
        }
        .map_err(|_| "The binary exports no VST2 entry point (VSTPluginMain/main)".to_owned())?;
        // SAFETY: the symbol has the published VST2 entry ABI and the library remains mapped.
        unsafe { Self::from_entry(binary, Some(library), entry, requested_id) }
    }

    unsafe fn from_entry(
        binary: PathBuf,
        library: Option<Arc<Library>>,
        entry: Entry,
        requested_id: i32,
    ) -> Result<Self, String> {
        let mut host = HostContext::new();
        host.requested_id = requested_id;
        let mut shared = Arc::new(Shared {
            host,
            ui: Mutex::new(UiState::default()),
            owner: std::thread::current().id(),
            _library: library,
            binary,
            unique_id: 0,
            version: 0,
            flags: 0,
            retired: AtomicBool::new(false),
        });
        let _loading = LoadingGuard::new(&shared.host);
        let effect = NonNull::new(unsafe { entry(callback) })
            .ok_or("The VST2 entry point returned no effect instance")?;
        let result = unsafe { effect.as_ref() };
        if result.magic != MAGIC || result.dispatcher.is_none() {
            // An invalid ABI must never be dispatched into, even to attempt cleanup.
            return Err("The binary did not return a valid VST2 effect ABI".into());
        }
        // No other Arc reference exists yet; the entry's borrow has ended.
        let unique = Arc::get_mut(&mut shared).ok_or("VST2 initialization ownership failed")?;
        unique.unique_id = result.unique_id;
        unique.version = result.version;
        unique.flags = result.flags;
        lock(&unique.ui).effect = effect.as_ptr() as usize;
        unsafe {
            (*effect.as_ptr()).reserved1 = &unique.host as *const HostContext as isize;
        }
        let mut instance = Self {
            effect,
            shared,
            inputs: 0,
            outputs: 0,
            active: false,
            offline: false,
        };
        unsafe {
            dispatch(effect, OPEN, 0, 0, ptr::null_mut(), 0.0);
        }
        // Some shells finish creating the selected child during effOpen. Freeze the actual
        // child's metadata only after that call; requested_id stays independently available
        // through the per-instance callback for the entire lifetime.
        let actual = unsafe { effect.as_ref() };
        let unique =
            Arc::get_mut(&mut instance.shared).ok_or("VST2 initialization ownership failed")?;
        unique.unique_id = actual.unique_id;
        unique.version = actual.version;
        unique.flags = actual.flags;
        if requested_id != 0 && actual.unique_id != requested_id {
            return Err(format!(
                "The VST2 binary returned {:08X} instead of requested plugin {:08X}",
                actual.unique_id as u32, requested_id as u32
            ));
        }
        if unsafe { dispatch(effect, API_VERSION, 0, 0, ptr::null_mut(), 0.0) } < 2400 {
            return Err("Only VST 2.4 plugins are supported".into());
        }
        let parameters = unsafe { effect.as_ref().parameters };
        if !(0..=MAX_PARAMS as i32).contains(&parameters) {
            return Err("The VST2 parameter count exceeds the supported limit".into());
        }
        instance
            .shared
            .host
            .count
            .store(parameters as usize, Ordering::Relaxed);
        if parameters > 0
            && (unsafe {
                effect.as_ref().get_parameter.is_none() || effect.as_ref().set_parameter.is_none()
            })
        {
            return Err("The VST2 plugin has no valid parameter callbacks".into());
        }
        instance.shared.read_values(effect, false);
        Ok(instance)
    }

    pub(crate) fn scan_descriptors(&self, path: &Path) -> Result<Vec<Vst2Descriptor>, String> {
        let container = self.descriptor(path);
        if container.raw_category != Some(10) {
            return Ok(vec![container]);
        }
        let mut seen = HashSet::new();
        let mut children = Vec::new();
        // One extra dispatch distinguishes a valid list of exactly MAX children from a list
        // with no terminator. No child entry point is invoked while scanning the container.
        for index in 0..=MAX_SHELL_CHILDREN {
            let mut name = [0u8; 256];
            let raw =
                unsafe { dispatch(self.effect, SHELL_NEXT, 0, 0, name.as_mut_ptr().cast(), 0.0) };
            if raw == 0 {
                return Ok(children);
            }
            let id = shell_child_id(raw).ok_or_else(|| {
                format!(
                    "VST2 shell returned an invalid child identifier at entry {}",
                    index + 1
                )
            })?;
            if index == MAX_SHELL_CHILDREN {
                return Err(format!(
                    "VST2 shell exceeds the limit of {MAX_SHELL_CHILDREN} child plugins"
                ));
            }
            if !seen.insert(id) {
                return Err(format!(
                    "VST2 shell repeated child identifier {:08X}",
                    id as u32
                ));
            }
            let length = name
                .iter()
                .position(|byte| *byte == 0)
                .unwrap_or(name.len());
            let name: String = String::from_utf8_lossy(&name[..length])
                .chars()
                .filter(|character| !character.is_control())
                .collect();
            let name = if name.trim().is_empty() {
                format!("VST2 {:08X}", id as u32)
            } else {
                name.trim().to_owned()
            };
            children.push(Vst2Descriptor {
                id: format!("vst2:{:08X}", id as u32),
                name,
                vendor: container.vendor.clone(),
                // The shell iterator reports child names and IDs only. Version/category are
                // deliberately unknown until selected instantiation, rather than parent's data.
                version: String::new(),
                path: path.to_path_buf(),
                raw_category: None,
                is_instrument: None,
                is_shell_child: true,
            });
        }
        Err("The VST2 shell child list did not terminate".into())
    }

    pub(crate) fn descriptor(&self, path: &Path) -> Vst2Descriptor {
        let mut name = unsafe { read_text(self.effect, EFFECT_NAME, 0, 32) };
        if name.is_empty() {
            name = unsafe { read_text(self.effect, PRODUCT, 0, 64) };
        }
        if name.is_empty() {
            name = path.file_stem().map_or_else(
                || "VST2 Effect".to_owned(),
                |name| name.to_string_lossy().into_owned(),
            );
        }
        let raw_category =
            i32::try_from(unsafe { dispatch(self.effect, CATEGORY, 0, 0, ptr::null_mut(), 0.0) })
                .ok();
        Vst2Descriptor {
            id: format!("vst2:{:08X}", self.shared.unique_id as u32),
            name,
            vendor: unsafe { read_text(self.effect, VENDOR, 0, 64) },
            version: self.shared.version.to_string(),
            path: path.to_path_buf(),
            raw_category,
            is_instrument: Some(self.shared.flags & IS_SYNTH != 0 || raw_category == Some(2)),
            is_shell_child: self.shared.host.requested_id != 0,
        }
    }

    pub(crate) fn validate_effect(&self) -> Result<(), String> {
        let effect = unsafe { self.effect.as_ref() };
        if unsafe { dispatch(self.effect, CATEGORY, 0, 0, ptr::null_mut(), 0.0) } == 10 {
            return Err(
                "A VST2 shell container cannot process audio; select one of its child plugins"
                    .into(),
            );
        }
        if self.shared.flags & IS_SYNTH != 0
            || unsafe { dispatch(self.effect, CATEGORY, 0, 0, ptr::null_mut(), 0.0) } == 2
        {
            return Err("VST2 instruments are not supported by this audio-effect host".into());
        }
        // VST2 has a single input bus. Effects with a sidechain widen it beyond the stereo main
        // pair (4, 6 or 8 pins); those extra pins are fed silence.
        if !(1..=MAX_INPUT_PINS as i32).contains(&effect.inputs)
            || !(1..=MAIN_PINS as i32).contains(&effect.outputs)
        {
            return Err(
                "Only VST2 effects with mono or stereo outputs and up to 64 inputs are supported"
                    .into(),
            );
        }
        if effect.flags & CAN_REPLACING == 0 || effect.process_replacing.is_none() {
            return Err("The VST2 plugin does not support 32-bit replacing processing".into());
        }
        Ok(())
    }

    pub(crate) fn shared(&self) -> Arc<Shared> {
        Arc::clone(&self.shared)
    }

    pub(crate) fn plugin_info(&self) -> Result<&'static PluginInfo, String> {
        static INFOS: OnceLock<Mutex<HashMap<String, &'static PluginInfo>>> = OnceLock::new();
        let descriptor = self.descriptor(Path::new(""));
        let key = format!(
            "{}:{}:{}:{}",
            self.shared.binary.display(),
            descriptor.id,
            self.shared.version,
            self.shared.host.count.load(Ordering::Relaxed)
        );
        let mut cache = lock(INFOS.get_or_init(Mutex::default));
        if let Some(info) = cache.get(&key) {
            return Ok(*info);
        }
        if cache.len() >= 4096 {
            return Err("The VST2 plugin metadata registry is full".into());
        }
        let mut parameters = Vec::new();
        for index in 0..self.shared.host.count.load(Ordering::Relaxed) {
            let mut name = unsafe { read_text(self.effect, PARAM_NAME, index as i32, 32) };
            if name.is_empty() {
                name = format!("Parameter {}", index + 1);
            }
            parameters.push(ParamInfo {
                id: Box::leak(index.to_string().into_boxed_str()),
                name: Box::leak(name.into_boxed_str()),
                min: 0.0,
                max: 1.0,
                default: f32::from_bits(self.shared.host.values[index].load(Ordering::Relaxed)),
                unit: Unit::None,
                taper: Taper::Linear,
                choices: &[],
            });
        }
        let short_name: String = descriptor.name.chars().take(8).collect();
        let info = Box::leak(Box::new(PluginInfo {
            id: Box::leak(descriptor.id.into_boxed_str()),
            name: Box::leak(descriptor.name.into_boxed_str()),
            short_name: Box::leak(short_name.into_boxed_str()),
            category: Category::Other,
            params: Box::leak(parameters.into_boxed_slice()),
            is_instrument: false,
            audiosuite: true,
        }));
        cache.insert(key, info);
        Ok(info)
    }

    pub(crate) fn prepare(&mut self, sample_rate: f32, block: usize) -> Result<(), String> {
        // Setup/reset are control operations; unlike process, they may briefly wait for an
        // editor idle call to finish before changing the plugin's processing lifecycle.
        let _ui = lock(&self.shared.ui);
        self.validate_effect()?;
        if self.active {
            unsafe {
                dispatch(self.effect, STOP, 0, 0, ptr::null_mut(), 0.0);
                dispatch(self.effect, MAINS, 0, 0, ptr::null_mut(), 0.0);
            }
            self.active = false;
            self.shared.host.active.store(false, Ordering::Relaxed);
        }
        self.shared
            .host
            .sample_rate
            .store(f64::from(sample_rate).to_bits(), Ordering::Relaxed);
        self.shared.host.block_size.store(block, Ordering::Relaxed);
        unsafe {
            dispatch(self.effect, SAMPLE_RATE, 0, 0, ptr::null_mut(), sample_rate);
            dispatch(
                self.effect,
                BLOCK_SIZE,
                0,
                block as isize,
                ptr::null_mut(),
                0.0,
            );
            dispatch(self.effect, PRECISION, 0, 0, ptr::null_mut(), 0.0);
            dispatch(self.effect, MAINS, 0, 1, ptr::null_mut(), 0.0);
            dispatch(self.effect, START, 0, 0, ptr::null_mut(), 0.0);
        }
        self.active = true;
        self.shared.host.active.store(true, Ordering::Relaxed);
        self.validate_effect()?;
        let effect = unsafe { self.effect.as_ref() };
        self.inputs = effect.inputs as usize;
        self.outputs = effect.outputs as usize;
        self.shared
            .host
            .configured_inputs
            .store(self.inputs, Ordering::Relaxed);
        self.shared
            .host
            .configured_outputs
            .store(self.outputs, Ordering::Relaxed);
        self.shared
            .host
            .latency
            .store(effect.initial_delay.max(0) as usize, Ordering::Relaxed);
        self.shared
            .host
            .invalid_layout
            .store(false, Ordering::Relaxed);
        self.shared.host.io_changed.store(false, Ordering::Release);
        Ok(())
    }

    pub(crate) fn channels(&self) -> (usize, usize) {
        (self.inputs, self.outputs)
    }

    pub(crate) fn is_offline(&self) -> bool {
        self.offline
    }

    pub(crate) fn set_offline(&mut self, offline: bool) -> Result<(), String> {
        // This is a stopped control operation. Waiting for editor idle here does not put a
        // lock in processing; all render/parameter flush paths retain their lock-free contract.
        let _ui = lock(&self.shared.ui);
        if self.shared.retired.load(Ordering::Acquire) {
            return Err("The VST2 instance is closed".into());
        }
        self.validate_effect()?;
        let effect = unsafe { self.effect.as_ref() };
        if effect.parameters as usize != self.shared.host.count.load(Ordering::Relaxed) {
            return Err("The VST2 plugin changed its parameter layout; reload the plugin".into());
        }
        if self.active {
            self.check_layout()?;
            // The worker is stopped, so also reject an unannounced bus change instead of
            // reporting successful mode switching for an invalid prepared instance.
            if effect.inputs as usize != self.inputs || effect.outputs as usize != self.outputs {
                return Err("The VST2 plugin changed its channel layout; reload the plugin".into());
            }
        }
        self.offline = offline;
        Ok(())
    }
    pub(crate) fn sample_rate(&self) -> f64 {
        f64::from_bits(self.shared.host.sample_rate.load(Ordering::Relaxed))
    }
    pub(crate) fn latency(&self) -> usize {
        // The release/acquire notification also publishes a UI-originated latency write.
        if self.shared.host.io_changed.load(Ordering::Acquire) {
            let latency = unsafe { self.effect.as_ref().initial_delay.max(0) as usize };
            self.shared.host.latency.store(latency, Ordering::Relaxed);
        }
        self.shared.host.latency.load(Ordering::Relaxed)
    }

    pub(crate) fn latency_control(&self) -> usize {
        // Control-only: the worker is quiesced, so read the latest value even if a plugin
        // changes its delay while applying a preset without sending an IO notification.
        let latency = unsafe { self.effect.as_ref().initial_delay.max(0) as usize };
        self.shared.host.latency.store(latency, Ordering::Relaxed);
        latency
    }

    pub(crate) fn tail(&self) -> usize {
        let tail = unsafe { dispatch(self.effect, TAIL, 0, 0, ptr::null_mut(), 0.0) };
        if tail <= 1 { 0 } else { tail as usize }
    }

    pub(crate) fn check_layout(&self) -> Result<(), String> {
        if self.shared.host.io_changed.swap(false, Ordering::Acquire) {
            let effect = unsafe { self.effect.as_ref() };
            self.shared
                .host
                .latency
                .store(effect.initial_delay.max(0) as usize, Ordering::Relaxed);
            if effect.inputs as usize != self.inputs
                || effect.outputs as usize != self.outputs
                || effect.parameters as usize != self.shared.host.count.load(Ordering::Relaxed)
            {
                self.shared
                    .host
                    .invalid_layout
                    .store(true, Ordering::Relaxed);
            }
        }
        if self.shared.host.invalid_layout.load(Ordering::Acquire) {
            return Err(
                "The VST2 plugin changed its channel or parameter layout; reload the plugin".into(),
            );
        }
        Ok(())
    }

    pub(crate) fn flush_pending(&self) {
        let _level = ProcessLevelGuard::processing(self.offline);
        self.shared.flush(self.effect);
    }

    pub(crate) fn process(
        &mut self,
        inputs: &[Vec<f32>],
        outputs: &mut [Vec<f32>],
        frames: usize,
        transport: &Transport,
    ) {
        let _context = LoadingGuard::new(&self.shared.host);
        self.shared.host.update_transport(transport);
        // Fixed-size pointer tables keep the audio callback free of allocations.
        let mut input_pointers = [ptr::null::<f32>(); MAX_INPUT_PINS];
        for (pointer, channel) in input_pointers.iter_mut().zip(inputs) {
            *pointer = channel.as_ptr();
        }
        let mut output_pointers = [ptr::null_mut::<f32>(); MAIN_PINS];
        for (pointer, channel) in output_pointers.iter_mut().zip(outputs.iter_mut()) {
            *pointer = channel.as_mut_ptr();
        }
        if let Some(process) = unsafe { self.effect.as_ref().process_replacing } {
            let _level = ProcessLevelGuard::processing(self.offline);
            unsafe {
                process(
                    self.effect.as_ptr(),
                    input_pointers.as_ptr(),
                    output_pointers.as_mut_ptr(),
                    frames as i32,
                );
            }
            self.shared.host.expected_samples.store(
                transport.samples.saturating_add(frames as i64),
                Ordering::Relaxed,
            );
        }
    }

    pub(crate) fn reset(&mut self) {
        let _ui = lock(&self.shared.ui);
        self.shared
            .host
            .expected_samples
            .store(i64::MIN, Ordering::Relaxed);
        if self.active {
            unsafe {
                dispatch(self.effect, STOP, 0, 0, ptr::null_mut(), 0.0);
                dispatch(self.effect, MAINS, 0, 0, ptr::null_mut(), 0.0);
                dispatch(self.effect, MAINS, 0, 1, ptr::null_mut(), 0.0);
                dispatch(self.effect, START, 0, 0, ptr::null_mut(), 0.0);
            }
        }
    }

    pub(crate) fn save_state(&self) -> Option<Vec<u8>> {
        self.shared.save()
    }

    pub(crate) fn load_state(&mut self, bytes: &[u8]) -> bool {
        let count = self.shared.host.count.load(Ordering::Relaxed);
        let Some(state) = decode_state(bytes, self.shared.unique_id, self.shared.version, count)
        else {
            return false;
        };
        if !self.shared.is_owner() {
            return false;
        }
        let _context = LoadingGuard::new(&self.shared.host);
        let _level = ProcessLevelGuard::user();
        match state {
            State::Chunk(data) if self.shared.flags & PROGRAM_CHUNKS != 0 => {
                // Plugins may retain/mutate input only for the duration of effSetChunk.
                let mut data = data.to_vec();
                let accepted = unsafe {
                    dispatch(
                        self.effect,
                        SET_CHUNK,
                        0,
                        data.len() as isize,
                        data.as_mut_ptr().cast(),
                        0.0,
                    )
                };
                // VST2 effSetChunk has no required positive success return. A negative result
                // is an explicit rejection; zero is used by many conforming implementations.
                if accepted < 0 {
                    return false;
                }
            }
            State::Parameters(data) if self.shared.flags & PROGRAM_CHUNKS == 0 => {
                let Some(set) = (unsafe { self.effect.as_ref().set_parameter }) else {
                    return count == 0;
                };
                for (index, value) in data.chunks_exact(4).enumerate() {
                    let value = f32::from_le_bytes([value[0], value[1], value[2], value[3]]);
                    unsafe {
                        set(self.effect.as_ptr(), index as i32, value);
                    }
                }
            }
            _ => return false,
        }
        for pending in self.shared.host.pending.iter().take(count) {
            pending.store(NONE, Ordering::Relaxed);
        }
        for edit in self.shared.host.edits.iter().take(count) {
            edit.store(NONE, Ordering::Relaxed);
        }
        self.shared.read_values(self.effect, false);
        true
    }

    pub(crate) fn open_editor(&self, name: &str) -> Result<(), String> {
        self.shared.open(name)
    }
    pub(crate) fn close_editor(&self) {
        self.shared.close_editor();
    }

    pub(crate) fn editor(&self, name: &'static str) -> Box<dyn PluginEditor> {
        Box::new(Editor {
            shared: Arc::downgrade(&self.shared),
            name,
        })
    }

    pub(crate) fn state_reader(&self) -> Box<dyn PluginStateReader> {
        let weak = Arc::downgrade(&self.shared);
        Box::new(move || weak.upgrade()?.save())
    }
}

impl Drop for Instance {
    fn drop(&mut self) {
        self.shared.retired.store(true, Ordering::Release);
        if self.shared.is_owner() {
            self.shared.close_now();
        } else {
            // Teardown is a control-path operation. No shutdown/window operation runs in a
            // processing callback, and no UI handle can enter after retired becomes true.
            let mut queue = lock(RETIRED.get_or_init(Mutex::default));
            if queue.len() < 4096 {
                queue.push(Arc::clone(&self.shared));
            } else {
                // A stopped UI loop must not cause a use-after-unload. The helper process owns
                // this bounded-failure leak until exit; normal UI pumping drains the queue.
                std::mem::forget(Arc::clone(&self.shared));
            }
        }
    }
}

struct Editor {
    shared: Weak<Shared>,
    name: &'static str,
}
impl PluginEditor for Editor {
    fn open(&mut self) -> Result<(), String> {
        self.shared
            .upgrade()
            .ok_or("The VST2 instance is closed")?
            .open(self.name)
    }
    fn close(&mut self) {
        if let Some(shared) = self.shared.upgrade() {
            shared.close_editor();
        }
    }
    fn is_open(&self) -> bool {
        self.shared.upgrade().is_some_and(|shared| {
            shared.is_owner()
                && !shared.retired.load(Ordering::Acquire)
                && lock(&shared.ui)
                    .window
                    .as_ref()
                    .is_some_and(NativeEditorWindow::is_visible)
        })
    }
    fn idle(&mut self) -> Vec<(String, f32)> {
        self.shared
            .upgrade()
            .map_or_else(Vec::new, |shared| shared.idle())
    }
    fn normalize(&self, id: &str, value: f32) -> Option<f64> {
        let shared = self.shared.upgrade()?;
        shared.host.index(id)?;
        (!shared.retired.load(Ordering::Acquire) && value.is_finite())
            .then(|| f64::from(value.clamp(0.0, 1.0)))
    }
    fn set_param(&mut self, id: &str, value: f32) {
        if let Some(shared) = self.shared.upgrade() {
            shared.queue(id, value);
        }
    }
    fn value_text(&mut self, id: &str, value: f32) -> Option<String> {
        let shared = self.shared.upgrade()?;
        let index = shared.host.index(id)?;
        // VST2 has no text-for-arbitrary-value call. Do not mutate a live plugin to format UI.
        if (shared.value(id)? - value).abs() > 1e-6
            || shared.host.pending[index].load(Ordering::Acquire) != NONE
        {
            return None;
        }
        shared
            .with_effect(|effect| {
                let display = unsafe { read_text(effect, PARAM_DISPLAY, index as i32, 32) };
                let label = unsafe { read_text(effect, PARAM_LABEL, index as i32, 32) };
                if display.is_empty() {
                    return None;
                }
                Some(if label.is_empty() || display.ends_with(&label) {
                    display
                } else {
                    format!("{display} {label}")
                })
            })
            .flatten()
    }
}

// A self-describing envelope binds data to the plugin ID, plugin version, parameter count,
// representation, and schema version before any bytes reach native effSetChunk.
const STATE_HEADER: usize = 28;
enum State<'a> {
    Chunk(&'a [u8]),
    Parameters(&'a [u8]),
}

fn encode_state(id: i32, version: i32, kind: u32, count: usize, payload: &[u8]) -> Vec<u8> {
    let mut result = Vec::with_capacity(STATE_HEADER + payload.len());
    result.extend_from_slice(b"PMV2");
    for number in [
        1u32,
        id as u32,
        version as u32,
        kind,
        count as u32,
        payload.len() as u32,
    ] {
        result.extend_from_slice(&number.to_le_bytes());
    }
    result.extend_from_slice(payload);
    result
}

fn decode_state(data: &[u8], id: i32, version: i32, count: usize) -> Option<State<'_>> {
    if data.len() < STATE_HEADER || data.len() > MAX_STATE || data.get(..4)? != b"PMV2" {
        return None;
    }
    let word = |at: usize| -> Option<u32> {
        Some(u32::from_le_bytes(data.get(at..at + 4)?.try_into().ok()?))
    };
    if word(4)? != 1
        || word(8)? != id as u32
        || word(12)? != version as u32
        || word(20)? as usize != count
        || count > MAX_PARAMS
    {
        return None;
    }
    let payload = data.get(STATE_HEADER..)?;
    if word(24)? as usize != payload.len() {
        return None;
    }
    match word(16)? {
        1 if !payload.is_empty() => Some(State::Chunk(payload)),
        2 if payload.len() == count.checked_mul(4)? => {
            for value in payload.chunks_exact(4) {
                let value = f32::from_le_bytes([value[0], value[1], value[2], value[3]]);
                if !value.is_finite() || !(0.0..=1.0).contains(&value) {
                    return None;
                }
            }
            Some(State::Parameters(payload))
        }
        _ => None,
    }
}

fn binary_path(path: &Path) -> Result<PathBuf, String> {
    if path.is_file() {
        return Ok(path.to_path_buf());
    }
    #[cfg(target_os = "macos")]
    if path.is_dir()
        && path
            .extension()
            .is_some_and(|extension| extension.eq_ignore_ascii_case("vst"))
    {
        let dictionary = plist::Value::from_file(path.join("Contents/Info.plist"))
            .map_err(|error| format!("Cannot read the VST2 bundle property list: {error}"))?;
        let executable = dictionary
            .as_dictionary()
            .and_then(|dictionary| dictionary.get("CFBundleExecutable"))
            .and_then(plist::Value::as_string)
            .ok_or("The VST2 bundle has no CFBundleExecutable")?;
        if executable.is_empty()
            || Path::new(executable).components().count() != 1
            || executable.contains(['/', '\\'])
        {
            return Err("The VST2 bundle executable name is invalid".into());
        }
        let binary = path.join("Contents/MacOS").join(executable);
        if binary.is_file() {
            return Ok(binary);
        }
        return Err("The VST2 bundle executable does not exist".into());
    }
    Err("The VST2 binary path does not exist or is not a supported bundle".into())
}

fn shell_child_id(raw: isize) -> Option<i32> {
    // Some C++ dispatchers sign-extend VstInt32 and others zero-extend its uint32 bit pattern.
    let raw = raw as i128;
    (raw != 0 && raw >= i128::from(i32::MIN) && raw <= i128::from(u32::MAX))
        .then_some(raw as u32 as i32)
}

#[cfg(test)]
mod tests {
    use super::*;
    use soundcraft_dsp::Plugin;

    #[derive(Default)]
    struct Events {
        opens: AtomicUsize,
        closes: AtomicUsize,
        starts: AtomicUsize,
        stops: AtomicUsize,
        sets: AtomicUsize,
        sample: AtomicU64,
        tempo: AtomicU64,
        flags: AtomicU32,
        frames: AtomicUsize,
        editor_opens: AtomicUsize,
        editor_parent: AtomicUsize,
        editor_value: AtomicI64,
        pre_return_id: AtomicI64,
        open_current_id: AtomicI64,
        set_current_id: AtomicI64,
        close_current_id: AtomicI64,
        process_level: AtomicI64,
        set_level: AtomicI64,
        get_level: AtomicI64,
        dispatch_level: AtomicI64,
        tail_queries: AtomicUsize,
        /// Largest absolute sample seen on input pins beyond the main pair, as f32 bits.
        sidechain_peak: AtomicU32,
    }

    struct Fake {
        parameters: [f32; 2],
        chunk: Vec<u8>,
        events: Arc<Events>,
        rectangle: Option<EditorRect>,
        category: isize,
        shell_children: Vec<(isize, String)>,
        shell_cursor: usize,
        repeat_last_child: bool,
    }

    unsafe extern "C" fn fake_dispatch(
        effect: *mut Effect,
        opcode: i32,
        index: i32,
        value: isize,
        pointer: *mut c_void,
        _opt: f32,
    ) -> isize {
        let fake = unsafe { &mut *((*effect).object as *mut Fake) };
        fake.events.dispatch_level.store(
            unsafe { callback(effect, 23, 0, 0, ptr::null_mut(), 0.0) } as i64,
            Ordering::Relaxed,
        );
        match opcode {
            OPEN => {
                fake.events.opens.fetch_add(1, Ordering::Relaxed);
                fake.events.open_current_id.store(
                    unsafe { callback(ptr::null_mut(), 2, 0, 0, ptr::null_mut(), 0.0) } as i64,
                    Ordering::Relaxed,
                );
                0
            }
            CLOSE => {
                fake.events.closes.fetch_add(1, Ordering::Relaxed);
                fake.events.close_current_id.store(
                    unsafe { callback(ptr::null_mut(), 2, 0, 0, ptr::null_mut(), 0.0) } as i64,
                    Ordering::Relaxed,
                );
                unsafe {
                    drop(Box::from_raw((*effect).object as *mut Fake));
                    drop(Box::from_raw(effect));
                }
                0
            }
            API_VERSION => 2400,
            CATEGORY => fake.category,
            SHELL_NEXT => {
                let entry = fake.shell_children.get(fake.shell_cursor).or_else(|| {
                    fake.repeat_last_child
                        .then(|| fake.shell_children.last())
                        .flatten()
                });
                let Some((id, name)) = entry else {
                    return 0;
                };
                unsafe {
                    copy_string(pointer, name.as_bytes(), 64);
                }
                fake.shell_cursor += 1;
                *id
            }
            EDIT_RECT => {
                let rectangle = fake
                    .rectangle
                    .as_mut()
                    .map_or(ptr::null_mut(), ptr::from_mut);
                unsafe {
                    pointer.cast::<*mut EditorRect>().write(rectangle);
                }
                isize::from(!rectangle.is_null())
            }
            EDIT_OPEN => {
                fake.events.editor_opens.fetch_add(1, Ordering::Relaxed);
                fake.events
                    .editor_parent
                    .store(pointer as usize, Ordering::Relaxed);
                fake.events
                    .editor_value
                    .store(value as i64, Ordering::Relaxed);
                fake.rectangle = Some(EditorRect {
                    left: 0,
                    top: 0,
                    right: 640,
                    bottom: 480,
                });
                0
            }
            START => {
                fake.events.starts.fetch_add(1, Ordering::Relaxed);
                0
            }
            STOP => {
                fake.events.stops.fetch_add(1, Ordering::Relaxed);
                0
            }
            EFFECT_NAME => {
                unsafe {
                    copy_string(pointer, b"Gain", 32);
                }
                1
            }
            PARAM_NAME => {
                unsafe {
                    copy_string(pointer, if index == 0 { b"Gain" } else { b"Mix" }, 32);
                }
                1
            }
            PARAM_DISPLAY => {
                unsafe {
                    copy_string(pointer, b"50", 32);
                }
                1
            }
            PARAM_LABEL => {
                unsafe {
                    copy_string(pointer, b"%", 32);
                }
                1
            }
            GET_CHUNK => {
                unsafe {
                    pointer.cast::<*mut u8>().write(fake.chunk.as_mut_ptr());
                }
                fake.chunk.len() as isize
            }
            SET_CHUNK => {
                fake.chunk =
                    unsafe { std::slice::from_raw_parts(pointer.cast::<u8>(), value as usize) }
                        .to_vec();
                0
            }
            TAIL => {
                fake.events.tail_queries.fetch_add(1, Ordering::Relaxed);
                (fake.parameters[0] * 1024.0).round() as isize
            }
            _ => 0,
        }
    }

    unsafe extern "C" fn fake_set(effect: *mut Effect, index: i32, value: f32) {
        let fake = unsafe { &mut *((*effect).object as *mut Fake) };
        fake.parameters[index as usize] = value;
        fake.events.sets.fetch_add(1, Ordering::Relaxed);
        fake.events.set_level.store(
            unsafe { callback(effect, 23, 0, 0, ptr::null_mut(), 0.0) } as i64,
            Ordering::Relaxed,
        );
        fake.events.set_current_id.store(
            unsafe { callback(ptr::null_mut(), 2, 0, 0, ptr::null_mut(), 0.0) } as i64,
            Ordering::Relaxed,
        );
    }

    unsafe extern "C" fn fake_get(effect: *mut Effect, index: i32) -> f32 {
        let fake = unsafe { &*((*effect).object as *mut Fake) };
        fake.events.get_level.store(
            unsafe { callback(effect, 23, 0, 0, ptr::null_mut(), 0.0) } as i64,
            Ordering::Relaxed,
        );
        fake.parameters[index as usize]
    }

    unsafe extern "C" fn fake_process(
        effect: *mut Effect,
        inputs: *const *const f32,
        outputs: *mut *mut f32,
        frames: i32,
    ) {
        let fake = unsafe { &mut *((*effect).object as *mut Fake) };
        fake.events.process_level.store(
            unsafe { callback(effect, 23, 0, 0, ptr::null_mut(), 0.0) } as i64,
            Ordering::Relaxed,
        );
        let time = unsafe { callback(effect, 7, 0, 0, ptr::null_mut(), 0.0) } as *const TimeInfo;
        let time = unsafe { time.read() };
        fake.events
            .sample
            .store(time.sample_pos.to_bits(), Ordering::Relaxed);
        fake.events
            .tempo
            .store(time.tempo.to_bits(), Ordering::Relaxed);
        fake.events
            .flags
            .store(time.flags as u32, Ordering::Relaxed);
        fake.events.frames.store(frames as usize, Ordering::Relaxed);
        let mut sidechain_peak = 0.0f32;
        for pin in MAIN_PINS..unsafe { (*effect).inputs } as usize {
            let input = unsafe { *inputs.add(pin) };
            for frame in 0..frames as usize {
                sidechain_peak = sidechain_peak.max(unsafe { input.add(frame).read() }.abs());
            }
        }
        fake.events
            .sidechain_peak
            .store(sidechain_peak.to_bits(), Ordering::Relaxed);
        for channel in 0..unsafe { (*effect).outputs } as usize {
            let input = unsafe { *inputs.add(channel.min((*effect).inputs as usize - 1)) };
            let output = unsafe { *outputs.add(channel) };
            for frame in 0..frames as usize {
                unsafe {
                    output
                        .add(frame)
                        .write(input.add(frame).read() * fake.parameters[0]);
                }
            }
        }
    }

    fn raw_effect(
        inputs: usize,
        outputs: usize,
        chunks: bool,
        events: Arc<Events>,
    ) -> NonNull<Effect> {
        let fake = Box::new(Fake {
            parameters: [0.5, 0.75],
            chunk: vec![1, 4, 9, 16],
            events,
            rectangle: None,
            category: 1,
            shell_children: Vec::new(),
            shell_cursor: 0,
            repeat_last_child: false,
        });
        let effect = Box::new(Effect {
            magic: MAGIC,
            dispatcher: Some(fake_dispatch),
            legacy_process: None,
            set_parameter: Some(fake_set),
            get_parameter: Some(fake_get),
            programs: 1,
            parameters: 2,
            inputs: inputs as i32,
            outputs: outputs as i32,
            flags: CAN_REPLACING | if chunks { PROGRAM_CHUNKS } else { 0 },
            reserved1: 0,
            reserved2: 0,
            initial_delay: 0,
            real_qualities: 0,
            off_qualities: 0,
            io_ratio: 1.0,
            object: Box::into_raw(fake).cast(),
            user: ptr::null_mut(),
            unique_id: 0x6761_696E,
            version: 7,
            process_replacing: Some(fake_process),
            process_replacing_f64: None,
            future: [0; 56],
        });
        NonNull::from(Box::leak(effect))
    }

    fn instance(inputs: usize, outputs: usize, chunks: bool) -> (Instance, Arc<Events>) {
        let events = Arc::new(Events::default());
        let mut shared = Arc::new(Shared {
            host: HostContext::new(),
            ui: Mutex::new(UiState::default()),
            owner: std::thread::current().id(),
            _library: None,
            binary: PathBuf::from("gain.dll"),
            unique_id: 0x6761_696E,
            version: 7,
            flags: CAN_REPLACING | if chunks { PROGRAM_CHUNKS } else { 0 },
            retired: AtomicBool::new(false),
        });
        let effect = raw_effect(inputs, outputs, chunks, Arc::clone(&events));
        unsafe {
            (*effect.as_ptr()).reserved1 = &shared.host as *const HostContext as isize;
        }
        let unique = Arc::get_mut(&mut shared).unwrap();
        lock(&unique.ui).effect = effect.as_ptr() as usize;
        shared.host.count.store(2, Ordering::Relaxed);
        shared.read_values(effect, false);
        unsafe {
            dispatch(effect, OPEN, 0, 0, ptr::null_mut(), 0.0);
        }
        (
            Instance {
                effect,
                shared,
                inputs: 0,
                outputs: 0,
                active: false,
                offline: false,
            },
            events,
        )
    }

    #[derive(Clone)]
    struct ShellFixture {
        children: Vec<(isize, String)>,
        repeat_last_child: bool,
        actual_id: Option<i32>,
        standalone_id: Option<i32>,
        instrument: Option<i32>,
        created: Arc<Mutex<Vec<i32>>>,
        events: Arc<Events>,
    }

    impl ShellFixture {
        fn new(children: Vec<(isize, String)>) -> Self {
            Self {
                children,
                repeat_last_child: false,
                actual_id: None,
                standalone_id: None,
                instrument: None,
                created: Arc::new(Mutex::new(Vec::new())),
                events: Arc::new(Events::default()),
            }
        }
        fn load(&self, requested: i32) -> Result<Instance, String> {
            let _configuration = ShellConfiguration::new(self.clone());
            unsafe {
                Instance::from_entry(PathBuf::from("shell.dll"), None, shell_entry, requested)
            }
        }
    }

    thread_local! {
        static SHELL_CONFIGURATION: std::cell::RefCell<Option<ShellFixture>> = const { std::cell::RefCell::new(None) };
    }

    struct ShellConfiguration(Option<ShellFixture>);
    impl ShellConfiguration {
        fn new(configuration: ShellFixture) -> Self {
            Self(SHELL_CONFIGURATION.with(|slot| slot.replace(Some(configuration))))
        }
    }
    impl Drop for ShellConfiguration {
        fn drop(&mut self) {
            SHELL_CONFIGURATION.with(|slot| slot.replace(self.0.take()));
        }
    }

    unsafe extern "C" fn shell_entry(host: HostCallback) -> *mut Effect {
        // This callback happens before any AEffect exists, precisely where shells select a child.
        let requested = unsafe { host(ptr::null_mut(), 2, 0, 0, ptr::null_mut(), 0.0) } as i32;
        let Some(configuration) = SHELL_CONFIGURATION.with(|slot| slot.borrow().clone()) else {
            return ptr::null_mut();
        };
        lock(&configuration.created).push(requested);
        if requested != 0
            && configuration.standalone_id.is_none()
            && !configuration
                .children
                .iter()
                .any(|(id, _)| shell_child_id(*id) == Some(requested))
        {
            return ptr::null_mut();
        }
        let effect = raw_effect(2, 2, false, Arc::clone(&configuration.events));
        let selected_id = configuration.standalone_id.unwrap_or(if requested == 0 {
            0x5348_454C
        } else {
            requested
        });
        let actual = configuration.actual_id.unwrap_or(selected_id);
        unsafe {
            (*effect.as_ptr()).unique_id = actual;
        }
        let is_container = requested == 0 && configuration.standalone_id.is_none();
        let fake = unsafe { &mut *((*effect.as_ptr()).object as *mut Fake) };
        fake.category = if is_container {
            10
        } else if configuration.instrument == Some(requested) {
            2
        } else {
            1
        };
        fake.shell_children = configuration.children.clone();
        fake.repeat_last_child = configuration.repeat_last_child;
        if requested != 0 {
            fake.parameters[0] = if requested < 0 { 0.75 } else { 0.25 };
        }
        // An allocated-but-not-returned AEffect still has no reserved1 host pointer.
        let observed = unsafe { host(effect.as_ptr(), 2, 0, 0, ptr::null_mut(), 0.0) };
        configuration
            .events
            .pre_return_id
            .store(observed as i64, Ordering::Relaxed);
        effect.as_ptr()
    }

    #[test]
    fn standalone_scan_preserves_raw_dispatch_categories() {
        for category in [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, -1] {
            let (instance, _) = instance(2, 2, false);
            // SAFETY: this instance owns the fake effect and no processing thread is running.
            unsafe {
                let fake = &mut *((*instance.effect.as_ptr()).object as *mut Fake);
                fake.category = category;
            }
            let descriptors = instance.scan_descriptors(Path::new("gain.dll")).unwrap();
            assert_eq!(descriptors.len(), 1);
            assert_eq!(descriptors[0].raw_category, Some(category as i32));
            assert_eq!(descriptors[0].is_instrument, Some(category == 2));
            assert!(!descriptors[0].is_shell_child);
        }
    }

    #[test]
    fn shell_scan_enumerates_children_without_entering_each_child() {
        let first = 0x1234_5678i32;
        let second = 0x8765_4321u32 as i32;
        let fixture = ShellFixture::new(vec![
            (first as isize, "Filter".into()),
            (second as isize, "Delay".into()),
        ]);
        let container = fixture.load(0).unwrap();
        let descriptors = container.scan_descriptors(Path::new("shell.dll")).unwrap();
        assert_eq!(descriptors.len(), 2);
        assert_eq!(descriptors[0].id, "vst2:12345678");
        assert_eq!(descriptors[1].id, "vst2:87654321");
        assert!(
            descriptors
                .iter()
                .all(|descriptor| descriptor.is_shell_child
                    && descriptor.is_instrument.is_none()
                    && descriptor.raw_category.is_none()
                    && descriptor.version.is_empty())
        );
        assert_eq!(
            *lock(&fixture.created),
            vec![0],
            "scanning does not construct child instances"
        );
        assert!(
            container
                .validate_effect()
                .unwrap_err()
                .contains("select one of its child")
        );
        assert!(
            container
                .scan_descriptors(Path::new("shell.dll"))
                .unwrap()
                .is_empty(),
            "termination stays terminated"
        );
        drop(container);
        assert_eq!(fixture.events.opens.load(Ordering::Relaxed), 1);
        assert_eq!(fixture.events.closes.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn shell_children_select_through_entry_context_and_keep_independent_states() {
        let first = 0x1234_5678i32;
        let second = 0x8765_4321u32 as i32;
        let fixture = ShellFixture::new(vec![
            (first as isize, "Filter".into()),
            (second as isize, "Delay".into()),
        ]);
        let mut a = fixture.load(first).unwrap();
        assert_eq!(
            fixture.events.pre_return_id.load(Ordering::Relaxed),
            first as i64
        );
        assert_eq!(
            fixture.events.open_current_id.load(Ordering::Relaxed),
            first as i64
        );
        let b = fixture.load(second).unwrap();
        assert_eq!(
            fixture.events.pre_return_id.load(Ordering::Relaxed),
            second as i64
        );
        assert_eq!(
            fixture.events.open_current_id.load(Ordering::Relaxed),
            second as i64
        );
        assert_eq!(
            unsafe { callback(a.effect.as_ptr(), 2, 0, 0, ptr::null_mut(), 0.0) },
            first as isize
        );
        assert_eq!(
            unsafe { callback(b.effect.as_ptr(), 2, 0, 0, ptr::null_mut(), 0.0) },
            second as isize
        );
        assert_eq!(
            unsafe { callback(ptr::null_mut(), 2, 0, 0, ptr::null_mut(), 0.0) },
            0
        );
        assert!(a.validate_effect().is_ok());
        assert!(b.validate_effect().is_ok());
        assert_eq!(a.shared.value("0"), Some(0.25));
        assert_eq!(b.shared.value("0"), Some(0.75));
        let a_saved = a.save_state().unwrap();
        let b_saved = b.save_state().unwrap();
        assert!(
            !a.load_state(&b_saved),
            "a different child preset never reaches native setters"
        );
        a.shared.queue("0", 0.9);
        a.flush_pending();
        assert!(a.load_state(&a_saved));
        assert_eq!(
            fixture.events.set_current_id.load(Ordering::Relaxed),
            first as i64,
            "parameter-state setters retain the selected child callback context"
        );
        assert_eq!(a.shared.value("0"), Some(0.25));
        assert_eq!(b.shared.value("0"), Some(0.75));
        let mut reader = b.state_reader();
        let mut editor = b.editor("Delay");
        drop(a);
        assert!(reader.save_state().is_some());
        assert_eq!(editor.normalize("0", 0.5), Some(0.5));
        drop(b);
        assert!(reader.save_state().is_none());
        assert!(editor.open().is_err());
        assert_eq!(*lock(&fixture.created), vec![first, second]);
        assert_eq!(fixture.events.closes.load(Ordering::Relaxed), 2);
    }

    #[test]
    fn shell_repeated_ids_and_missing_terminators_are_bounded_errors() {
        let mut fixture = ShellFixture::new(vec![(1, "One".into()), (2, "Two".into())]);
        fixture.repeat_last_child = true;
        let container = fixture.load(0).unwrap();
        assert!(
            container
                .scan_descriptors(Path::new("shell.dll"))
                .unwrap_err()
                .contains("repeated child identifier")
        );
        let duplicates = ShellFixture::new(vec![(1, "One".into()), (1, "Other".into())]);
        assert!(
            duplicates
                .load(0)
                .unwrap()
                .scan_descriptors(Path::new("shell.dll"))
                .is_err()
        );
        let too_many = ShellFixture::new(
            (1..=MAX_SHELL_CHILDREN + 1)
                .map(|id| (id as isize, id.to_string()))
                .collect(),
        );
        assert!(
            too_many
                .load(0)
                .unwrap()
                .scan_descriptors(Path::new("shell.dll"))
                .unwrap_err()
                .contains("limit of 4096")
        );
        let exact = ShellFixture::new(
            (1..=MAX_SHELL_CHILDREN)
                .map(|id| (id as isize, id.to_string()))
                .collect(),
        );
        assert_eq!(
            exact
                .load(0)
                .unwrap()
                .scan_descriptors(Path::new("shell.dll"))
                .unwrap()
                .len(),
            MAX_SHELL_CHILDREN
        );
    }

    #[test]
    fn selected_child_identity_is_checked_before_it_can_be_used() {
        let fixture = ShellFixture::new(vec![(1, "One".into()), (2, "Two".into())]);
        assert!(
            fixture.load(3).is_err(),
            "a shell can refuse an unrecognized selector"
        );
        let mut wrong = fixture.clone();
        wrong.actual_id = Some(2);
        assert!(
            wrong
                .load(1)
                .err()
                .unwrap()
                .contains("instead of requested plugin")
        );
        assert_eq!(
            fixture.events.closes.load(Ordering::Relaxed),
            1,
            "a wrong constructed child is closed once"
        );
        let mut instrument = fixture.clone();
        instrument.instrument = Some(2);
        let child = instrument.load(2).unwrap();
        assert_eq!(
            child.descriptor(Path::new("shell.dll")).is_instrument,
            Some(true)
        );
        assert!(child.validate_effect().unwrap_err().contains("instruments"));
    }

    #[test]
    fn a_retired_shell_child_closes_with_its_selection_context_on_the_owner() {
        let id = 0x1234_5678;
        let fixture = ShellFixture::new(vec![(id as isize, "Filter".into())]);
        let child = fixture.load(id).unwrap();
        let mut reader = child.state_reader();
        std::thread::spawn(move || drop(child)).join().unwrap();
        assert_eq!(fixture.events.closes.load(Ordering::Relaxed), 0);
        assert!(reader.save_state().is_none());
        pump_ui();
        assert_eq!(fixture.events.closes.load(Ordering::Relaxed), 1);
        assert_eq!(
            fixture.events.close_current_id.load(Ordering::Relaxed),
            id as i64
        );
        pump_ui();
        assert_eq!(fixture.events.closes.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn signed_shell_ids_and_standalone_plugins_preserve_their_bit_patterns() {
        let id = 0x8765_4321u32 as i32;
        assert_eq!(shell_child_id(id as isize), Some(id));
        assert_eq!(shell_child_id(0x8765_4321u32 as isize), Some(id));
        assert_eq!(shell_child_id(0), None);
        if usize::BITS > 32 {
            assert_eq!(shell_child_id(isize::MAX), None);
        }
        let mut fixture = ShellFixture::new(Vec::new());
        fixture.standalone_id = Some(0x6761_696E);
        let standalone = fixture.load(0).unwrap();
        let descriptors = standalone.scan_descriptors(Path::new("gain.dll")).unwrap();
        assert_eq!(descriptors.len(), 1);
        assert_eq!(descriptors[0].id, "vst2:6761696E");
        assert_eq!(descriptors[0].is_instrument, Some(false));
        assert!(!descriptors[0].is_shell_child);
        assert!(standalone.validate_effect().is_ok());
        let selected = fixture.load(0x6761_696E).unwrap();
        assert!(selected.validate_effect().is_ok());
    }

    fn plugin(inputs: usize, outputs: usize) -> (crate::Vst2Plugin, Arc<Events>) {
        let (instance, events) = instance(inputs, outputs, false);
        let info = instance.plugin_info().unwrap();
        let pending = instance.shared();
        (
            crate::Vst2Plugin {
                instance,
                info,
                pending,
                inputs: Vec::new(),
                outputs: Vec::new(),
                max_block: 0,
                channels: 2,
                latency: 0,
                tail: 0,
                transport: Transport::default(),
                failure: None,
            },
            events,
        )
    }

    #[test]
    fn state_envelope_rejects_wrong_identity_version_and_trailing_data() {
        let payload = [0.25f32.to_le_bytes(), 0.75f32.to_le_bytes()].concat();
        let data = encode_state(42, 7, 2, 2, &payload);
        assert!(matches!(
            decode_state(&data, 42, 7, 2),
            Some(State::Parameters(_))
        ));
        assert!(decode_state(&data, 41, 7, 2).is_none());
        assert!(decode_state(&data, 42, 8, 2).is_none());
        assert!(decode_state(&data, 42, 7, 3).is_none());
        let mut trailing = data.clone();
        trailing.push(0);
        assert!(decode_state(&trailing, 42, 7, 2).is_none());
        for index in 0..data.len() {
            assert!(decode_state(&data[..index], 42, 7, 2).is_none());
        }
        let mut future_schema = data;
        future_schema[4] = 2;
        assert!(decode_state(&future_schema, 42, 7, 2).is_none());
    }

    #[test]
    fn parameter_state_validates_all_values_before_mutating_native_plugin() {
        let (mut instance, events) = instance(2, 2, false);
        let invalid = encode_state(
            instance.shared.unique_id,
            7,
            2,
            2,
            &[0.25f32.to_le_bytes(), f32::NAN.to_le_bytes()].concat(),
        );
        assert!(!instance.load_state(&invalid));
        assert_eq!(events.sets.load(Ordering::Relaxed), 0);
        assert_eq!(instance.shared.value("0"), Some(0.5));
        let saved = instance.save_state().unwrap();
        instance.shared.queue("0", 0.9);
        instance.flush_pending();
        assert!(instance.load_state(&saved));
        assert_eq!(instance.shared.value("0"), Some(0.5));
    }

    #[test]
    fn chunks_are_copied_and_zero_return_restore_is_accepted() {
        let (mut instance, _) = instance(2, 2, true);
        let saved = instance.save_state().unwrap();
        unsafe {
            (&mut *((*instance.effect.as_ptr()).object as *mut Fake))
                .chunk
                .fill(0);
        }
        assert_eq!(&saved[STATE_HEADER..], &[1, 4, 9, 16]);
        assert!(instance.load_state(&saved));
        assert_eq!(instance.save_state().unwrap(), saved);
    }

    #[test]
    fn normalized_queue_is_bounded_coalesced_and_not_applied_on_ui_thread() {
        let (instance, events) = instance(2, 2, false);
        for index in 0..100_000 {
            assert!(instance.shared.queue("0", index as f32 / 100_000.0));
        }
        assert_eq!(events.sets.load(Ordering::Relaxed), 0);
        assert!(!instance.shared.queue("4096", 0.4));
        assert!(!instance.shared.queue("+1", 0.4));
        assert!(!instance.shared.queue("01", 0.4));
        instance.flush_pending();
        assert_eq!(events.sets.load(Ordering::Relaxed), 1);
        assert_eq!(instance.shared.value("0"), Some(0.99999));
        instance.flush_pending();
        assert_eq!(events.sets.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn callbacks_route_to_the_correct_instance_and_coalesce_edits() {
        let (a, _) = instance(2, 2, false);
        let (b, _) = instance(2, 2, false);
        unsafe {
            callback(a.effect.as_ptr(), 0, 0, 0, ptr::null_mut(), 0.2);
            callback(b.effect.as_ptr(), 0, 0, 0, ptr::null_mut(), 0.8);
            callback(a.effect.as_ptr(), 0, 0, 0, ptr::null_mut(), 0.3);
            callback(a.effect.as_ptr(), 0, 4096, 0, ptr::null_mut(), 0.7);
            callback(a.effect.as_ptr(), 0, 1, 0, ptr::null_mut(), f32::NAN);
        }
        assert_eq!(a.shared.idle(), vec![("0".into(), 0.3)]);
        assert_eq!(b.shared.idle(), vec![("0".into(), 0.8)]);
        assert!(a.shared.idle().is_empty());
        assert_eq!(a.shared.value("1"), Some(0.75));
    }

    #[test]
    fn entry_point_context_is_scoped_and_restored() {
        let first = HostContext::new();
        let second = HostContext::new();
        first.block_size.store(128, Ordering::Relaxed);
        second.block_size.store(512, Ordering::Relaxed);
        let invoke = || unsafe { callback(ptr::null_mut(), 17, 0, 0, ptr::null_mut(), 0.0) };
        assert_eq!(invoke(), 0);
        {
            let _first = LoadingGuard::new(&first);
            assert_eq!(invoke(), 128);
            {
                let _second = LoadingGuard::new(&second);
                assert_eq!(invoke(), 512);
            }
            assert_eq!(invoke(), 128);
        }
        assert_eq!(invoke(), 0);
    }

    #[test]
    fn process_level_is_scoped_to_the_callback_thread() {
        let (instance, _) = instance(2, 2, false);
        let effect = instance.effect.as_ptr() as usize;
        let level = || unsafe { callback(effect as *mut Effect, 23, 0, 0, ptr::null_mut(), 0.0) };
        assert_eq!(level(), 1);
        {
            let _processing = ProcessLevelGuard::processing(false);
            assert_eq!(level(), 2);
            let ui_level = std::thread::spawn(move || unsafe {
                callback(effect as *mut Effect, 23, 0, 0, ptr::null_mut(), 0.0)
            })
            .join()
            .unwrap();
            assert_eq!(
                ui_level, 1,
                "another callback thread remains in user context"
            );
            {
                let _nested = ProcessLevelGuard::processing(false);
                assert_eq!(level(), 2);
            }
            assert_eq!(level(), 2);
        }
        assert_eq!(level(), 1);
    }

    #[test]
    fn offline_mode_reports_process_and_parameter_levels_without_replacing_the_instance() {
        let (mut plugin, events) = plugin(2, 2);
        let effect = plugin.instance.effect;
        let mut editor = plugin.editor().unwrap();
        let mut reader = plugin.state_reader().unwrap();
        assert!(!plugin.is_offline());
        plugin.set_offline(true).unwrap();
        assert!(plugin.is_offline());
        plugin.try_prepare(48_000.0, 4, 2).unwrap();
        assert!(
            plugin.is_offline(),
            "preparation keeps the selected processing mode"
        );
        plugin.set_param("0", 0.25);
        let mut audio = vec![vec![1.0; 4], vec![0.5; 4]];
        plugin.try_process(&mut audio, 4).unwrap();
        assert_eq!(events.process_level.load(Ordering::Relaxed), 4);
        assert_eq!(events.set_level.load(Ordering::Relaxed), 4);
        assert_eq!(audio, vec![vec![0.25; 4], vec![0.125; 4]]);
        assert!(reader.save_state().is_some());
        assert_eq!(events.get_level.load(Ordering::Relaxed), 1);
        assert_eq!(editor.value_text("0", 0.25), Some("50 %".into()));
        assert_eq!(events.dispatch_level.load(Ordering::Relaxed), 1);
        plugin.set_offline(false).unwrap();
        assert!(!plugin.is_offline());
        assert_eq!(plugin.instance.effect, effect);
        assert_eq!(plugin.param("0"), Some(0.25));
        assert_eq!(editor.normalize("0", 0.4), Some(f64::from(0.4f32)));
        assert!(reader.save_state().is_some());
        plugin.set_param("0", 0.75);
        plugin.flush_pending().unwrap();
        assert_eq!(events.set_level.load(Ordering::Relaxed), 2);
        plugin.try_process(&mut audio, 4).unwrap();
        assert_eq!(events.process_level.load(Ordering::Relaxed), 2);
    }

    #[test]
    fn processing_modes_are_scoped_across_instances_nested_calls_and_gui_threads() {
        let (mut offline, offline_events) = plugin(2, 2);
        let (mut realtime, realtime_events) = plugin(2, 2);
        offline.set_offline(true).unwrap();
        offline.try_prepare(48_000.0, 4, 2).unwrap();
        realtime.try_prepare(48_000.0, 4, 2).unwrap();
        let mut editor = offline.editor().unwrap();
        let effect = offline.instance.effect.as_ptr() as usize;
        let level = || unsafe { callback(effect as *mut Effect, 23, 0, 0, ptr::null_mut(), 0.0) };
        let mut audio = vec![vec![1.0; 4], vec![0.5; 4]];
        {
            let _outer = ProcessLevelGuard::processing(true);
            assert_eq!(level(), 4);
            realtime.try_process(&mut audio, 4).unwrap();
            assert_eq!(realtime_events.process_level.load(Ordering::Relaxed), 2);
            assert_eq!(level(), 4);
            {
                let _inner = ProcessLevelGuard::processing(false);
                offline.set_param("0", 0.25);
                offline.flush_pending().unwrap();
                assert_eq!(offline_events.set_level.load(Ordering::Relaxed), 4);
                assert_eq!(level(), 2);
                assert!(editor.value_text("0", 0.25).is_some());
                assert_eq!(offline_events.dispatch_level.load(Ordering::Relaxed), 1);
                assert_eq!(level(), 2);
            }
            assert_eq!(level(), 4);
            offline.try_process(&mut audio, 4).unwrap();
            assert_eq!(offline_events.process_level.load(Ordering::Relaxed), 4);
            let gui_level = std::thread::spawn(move || unsafe {
                callback(effect as *mut Effect, 23, 0, 0, ptr::null_mut(), 0.0)
            })
            .join()
            .unwrap();
            assert_eq!(gui_level, 1);
        }
        assert_eq!(level(), 1);
        assert!(offline.is_offline());
        assert!(!realtime.is_offline());
    }

    #[test]
    fn failed_mode_switch_preserves_the_mode_and_pending_parameter_state() {
        let (mut plugin, events) = plugin(2, 2);
        plugin.try_prepare(48_000.0, 4, 2).unwrap();
        plugin.set_offline(true).unwrap();
        plugin.set_param("0", 0.25);
        unsafe {
            (*plugin.instance.effect.as_ptr()).outputs = 4;
            callback(
                plugin.instance.effect.as_ptr(),
                13,
                0,
                0,
                ptr::null_mut(),
                0.0,
            );
        }
        assert!(plugin.set_offline(false).is_err());
        assert!(
            plugin.set_offline(true).is_err(),
            "a no-op request must still reject invalid state"
        );
        assert!(plugin.is_offline());
        assert_eq!(plugin.param("0"), Some(0.25));
        assert_ne!(plugin.pending.host.pending[0].load(Ordering::Acquire), NONE);
        assert_eq!(events.sets.load(Ordering::Relaxed), 0);
        assert!(
            plugin
                .try_process(&mut [vec![1.0; 4], vec![1.0; 4]], 4)
                .is_err()
        );
        assert_eq!(events.process_level.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn x11_editor_open_passes_the_numeric_xid_without_a_display_pointer() {
        let (instance, events) = instance(2, 2, false);
        let parent = 0x12AB_3400usize as *mut c_void;
        // No real X server/window is involved: the fake effect captures the ABI arguments.
        assert!(unsafe { attach_editor(instance.effect, parent, true) }.is_ok());
        assert_eq!(events.editor_opens.load(Ordering::Relaxed), 1);
        assert_eq!(
            events.editor_parent.load(Ordering::Relaxed),
            parent as usize
        );
        assert_eq!(events.editor_value.load(Ordering::Relaxed), 0);
        assert!(unsafe { attach_editor(instance.effect, ptr::null_mut(), true) }.is_err());
        if usize::BITS > 32 {
            let address = usize::MAX as *mut c_void;
            assert!(unsafe { attach_editor(instance.effect, address, true) }.is_err());
        }
        assert_eq!(events.editor_opens.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn editor_dimensions_can_be_queried_after_attachment_without_an_initial_rectangle() {
        let (instance, _) = instance(2, 2, false);
        assert_eq!(unsafe { editor_dimensions(instance.effect) }, None);
        unsafe { attach_editor(instance.effect, 0x1200usize as *mut c_void, true) }.unwrap();
        assert_eq!(
            unsafe { editor_dimensions(instance.effect) },
            Some((640, 480))
        );
        let fake = unsafe { &mut *((*instance.effect.as_ptr()).object as *mut Fake) };
        fake.rectangle = Some(EditorRect {
            top: 0,
            left: 100,
            bottom: 480,
            right: 50,
        });
        assert_eq!(unsafe { editor_dimensions(instance.effect) }, None);
    }

    #[test]
    fn an_unchanged_rectangle_does_not_undo_a_host_resize_request() {
        let mut state = UiState {
            dimensions: Some((400, 300)),
            reported_dimensions: Some((400, 300)),
            ..UiState::default()
        };
        assert_eq!(
            state.observe_dimensions(Some((720, 540)), Some((400, 300))),
            Some((720, 540))
        );
        assert_eq!(state.observe_dimensions(None, Some((400, 300))), None);
        assert_eq!(state.dimensions, Some((720, 540)));
        assert_eq!(
            state.observe_dimensions(None, Some((800, 600))),
            Some((800, 600))
        );
        assert_eq!(state.observe_dimensions(None, Some((800, 600))), None);
    }

    #[test]
    fn resize_callbacks_are_bounded_and_do_not_open_or_mutate_a_native_window() {
        let (instance, events) = instance(2, 2, false);
        let effect = instance.effect.as_ptr();
        assert_eq!(
            unsafe { callback(effect, 15, 720, 540, ptr::null_mut(), 0.0) },
            isize::from(NATIVE_EDITOR_SUPPORTED)
        );
        assert_eq!(instance.shared.host.width.load(Ordering::Relaxed), 720);
        assert_eq!(instance.shared.host.height.load(Ordering::Acquire), 540);
        assert_eq!(
            unsafe { callback(effect, 15, 0, 540, ptr::null_mut(), 0.0) },
            0
        );
        assert_eq!(
            unsafe { callback(effect, 15, 720, 16_385, ptr::null_mut(), 0.0) },
            0
        );
        assert_eq!(instance.shared.host.width.load(Ordering::Relaxed), 720);
        assert_eq!(instance.shared.host.height.load(Ordering::Acquire), 540);
        assert_eq!(events.editor_opens.load(Ordering::Relaxed), 0);
        let capability = c"sizeWindow";
        assert_eq!(
            unsafe { callback(effect, 37, 0, 0, capability.as_ptr().cast_mut().cast(), 0.0) },
            isize::from(NATIVE_EDITOR_SUPPORTED)
        );
    }

    #[test]
    fn opening_an_editor_on_a_worker_is_rejected_before_dispatch() {
        let (instance, events) = instance(2, 2, false);
        let mut editor = instance.editor("Gain");
        let error = std::thread::spawn(move || editor.open())
            .join()
            .unwrap()
            .unwrap_err();
        assert!(error.contains("owning UI thread"));
        assert_eq!(events.editor_opens.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn stereo_audio_is_processed_in_bounded_blocks_with_transport() {
        let (mut plugin, events) = plugin(2, 2);
        plugin.try_prepare(48_000.0, 2, 2).unwrap();
        plugin.set_transport(&Transport {
            playing: true,
            tempo: 96.0,
            numerator: 3,
            denominator: 4,
            samples: 120,
            quarters: 2.0,
            ..Transport::default()
        });
        let mut audio = vec![
            vec![1.0, 0.5, -0.5, -1.0, 0.2],
            vec![-1.0, -0.5, 0.5, 1.0, -0.2],
        ];
        plugin.try_process(&mut audio, 5).unwrap();
        assert_eq!(audio[0], vec![0.5, 0.25, -0.25, -0.5, 0.1]);
        assert_eq!(audio[1], vec![-0.5, -0.25, 0.25, 0.5, -0.1]);
        assert_eq!(f64::from_bits(events.sample.load(Ordering::Relaxed)), 124.0);
        assert_eq!(f64::from_bits(events.tempo.load(Ordering::Relaxed)), 96.0);
        assert_eq!(events.frames.load(Ordering::Relaxed), 1);
        assert_eq!(events.flags.load(Ordering::Relaxed) & 2, 2);
        assert_eq!(events.flags.load(Ordering::Relaxed) & 1, 0);
    }

    #[test]
    fn mono_effect_averages_stereo_input_and_duplicates_output() {
        let (mut plugin, _) = plugin(1, 1);
        plugin.try_prepare(48_000.0, 8, 2).unwrap();
        let mut audio = vec![vec![1.0, 1.0], vec![-1.0, 0.0]];
        plugin.try_process(&mut audio, 2).unwrap();
        assert_eq!(audio, vec![vec![0.0, 0.25], vec![0.0, 0.25]]);
    }

    #[test]
    fn invalid_prepare_and_multibus_effects_are_rejected() {
        let (mut plugin, _) = plugin(2, 2);
        assert!(plugin.try_prepare(f32::NAN, 256, 2).is_err());
        assert!(plugin.try_prepare(48_000.0, 0, 2).is_err());
        assert!(plugin.try_prepare(48_000.0, 256, 8).is_err());
        for (inputs, outputs) in [(2, 4), (MAX_INPUT_PINS + 1, 2), (0, 2)] {
            let (unsupported, _) = instance(inputs, outputs, false);
            assert!(unsupported.validate_effect().is_err(), "{inputs} in / {outputs} out");
        }
    }

    #[test]
    fn sidechain_pins_of_a_stereo_effect_receive_silence() {
        for (inputs, track_channels) in [(4, 2), (6, 2), (8, 2), (4, 1)] {
            let (mut plugin, events) = plugin(inputs, 2);
            plugin.try_prepare(48_000.0, 4, track_channels).unwrap();
            events.sidechain_peak.store(1.0f32.to_bits(), Ordering::Relaxed);
            let mut audio = vec![vec![1.0, -0.5, 0.25, 0.0]; track_channels];
            plugin.try_process(&mut audio, 4).unwrap();

            assert_eq!(audio[0], vec![0.5, -0.25, 0.125, 0.0], "{inputs} pins, {track_channels} ch");
            assert_eq!(f32::from_bits(events.sidechain_peak.load(Ordering::Relaxed)), 0.0);
        }
    }

    #[test]
    fn latency_notifications_are_applied_and_layout_changes_remain_rejected() {
        let (mut instance, _) = instance(2, 2, false);
        instance.prepare(48_000.0, 256).unwrap();
        unsafe {
            (*instance.effect.as_ptr()).initial_delay = 192;
            assert_eq!(
                callback(instance.effect.as_ptr(), 13, 0, 0, ptr::null_mut(), 0.0),
                1
            );
        }
        assert_eq!(instance.latency(), 192);
        assert!(instance.check_layout().is_ok());
        unsafe {
            (*instance.effect.as_ptr()).outputs = 4;
            assert_eq!(
                callback(instance.effect.as_ptr(), 13, 0, 0, ptr::null_mut(), 0.0),
                0
            );
        }
        assert!(instance.check_layout().is_err());
        assert!(instance.check_layout().is_err());
    }

    #[test]
    fn stopped_metadata_refresh_observes_tail_only_edits_without_realtime_property_queries() {
        let (mut plugin, events) = plugin(2, 2);
        plugin.try_prepare(48_000.0, 256, 2).unwrap();
        assert_eq!(plugin.latency(), 0);
        assert_eq!(plugin.tail_samples(), 512);
        let queries = events.tail_queries.load(Ordering::Relaxed);

        assert!(plugin.queue_normalized("0", 0.75, 0.75));
        plugin.flush_pending().unwrap();
        let mut io = vec![vec![1.0; 64]; 2];
        plugin.try_process(&mut io, 64).unwrap();
        assert_eq!(plugin.latency(), 0);
        assert_eq!(plugin.tail_samples(), 512);
        assert_eq!(events.tail_queries.load(Ordering::Relaxed), queries);

        assert_eq!(plugin.refresh_latency_control(), 0);
        assert_eq!(plugin.tail_samples(), 768);
        assert_eq!(events.tail_queries.load(Ordering::Relaxed), queries + 1);
    }

    #[test]
    fn editor_and_state_reader_handles_are_inert_after_drop() {
        let (instance, events) = instance(2, 2, false);
        let mut editor = instance.editor("Gain");
        let mut reader = instance.state_reader();
        assert_eq!(editor.normalize("0", 0.25), Some(0.25));
        assert!(reader.save_state().is_some());
        drop(instance);
        assert_eq!(events.closes.load(Ordering::Relaxed), 1);
        assert!(editor.open().is_err());
        assert_eq!(editor.normalize("0", 0.25), None);
        assert!(editor.idle().is_empty());
        assert!(reader.save_state().is_none());
    }

    #[test]
    fn worker_drop_defers_native_shutdown_until_owner_pumps() {
        let (instance, events) = instance(2, 2, false);
        let mut editor = instance.editor("Gain");
        std::thread::spawn(move || drop(instance)).join().unwrap();
        assert_eq!(events.closes.load(Ordering::Relaxed), 0);
        assert!(editor.open().is_err());
        pump_ui();
        assert_eq!(events.closes.load(Ordering::Relaxed), 1);
        pump_ui();
        assert_eq!(events.closes.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn ui_value_text_does_not_flush_pending_audio_parameter() {
        let (instance, events) = instance(2, 2, false);
        let mut editor = instance.editor("Gain");
        assert_eq!(editor.value_text("0", 0.5), Some("50 %".into()));
        editor.set_param("0", 0.3);
        assert_eq!(editor.value_text("0", 0.3), None);
        assert_eq!(events.sets.load(Ordering::Relaxed), 0);
        instance.flush_pending();
        assert_eq!(events.sets.load(Ordering::Relaxed), 1);
    }
}
