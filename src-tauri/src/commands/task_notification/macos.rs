use crate::error::{AppError, AppResult};
use block2::{DynBlock, RcBlock};
use objc2::rc::{autoreleasepool, Retained};
use objc2::runtime::{Bool, ProtocolObject};
use objc2::{define_class, msg_send, AnyThread};
use objc2_foundation::{NSBundle, NSError, NSObject, NSObjectProtocol, NSString, NSUUID};
use objc2_user_notifications::{
    UNAuthorizationOptions, UNMutableNotificationContent, UNNotification,
    UNNotificationPresentationOptions, UNNotificationRequest, UNNotificationSound,
    UNUserNotificationCenter, UNUserNotificationCenterDelegate,
};
use tauri::async_runtime::{channel, Receiver};

define_class!(
    #[unsafe(super(NSObject))]
    #[name = "PymssStudioTaskNotificationDelegate"]
    struct TaskNotificationDelegate;

    unsafe impl NSObjectProtocol for TaskNotificationDelegate {}

    unsafe impl UNUserNotificationCenterDelegate for TaskNotificationDelegate {
        #[unsafe(method(userNotificationCenter:willPresentNotification:withCompletionHandler:))]
        fn will_present(
            &self,
            _center: &UNUserNotificationCenter,
            _notification: &UNNotification,
            completion: &DynBlock<dyn Fn(UNNotificationPresentationOptions)>,
        ) {
            completion.call((UNNotificationPresentationOptions::Banner
                | UNNotificationPresentationOptions::List
                | UNNotificationPresentationOptions::Sound,));
        }
    }
);

thread_local! {
    // The notification center holds its delegate weakly.
    static DELEGATE: Retained<TaskNotificationDelegate> = {
        let allocated = TaskNotificationDelegate::alloc().set_ivars(());
        // SAFETY: NSObject's initializer accepts this subclass and initialized ivars.
        unsafe { msg_send![super(allocated), init] }
    };
}

pub(super) fn supported() -> bool {
    autoreleasepool(|_| {
        let bundle = NSBundle::mainBundle();
        bundle.bundleIdentifier().is_some()
            && bundle
                .bundleURL()
                .pathExtension()
                .is_some_and(|extension| extension.to_string().eq_ignore_ascii_case("app"))
    })
}

pub(crate) fn install_delegate() {
    if supported() {
        DELEGATE.with(|delegate| {
            UNUserNotificationCenter::currentNotificationCenter()
                .setDelegate(Some(ProtocolObject::from_ref(&**delegate)));
        });
    }
}

pub(super) async fn authorize() -> AppResult<()> {
    let completion = autoreleasepool(|_| {
        let (tx, rx) = channel(1);
        let callback = RcBlock::new(move |granted: Bool, error: *mut NSError| {
            // SAFETY: Apple supplies an optional borrowed NSError for the duration of the callback.
            let result = system_result(unsafe { error.as_ref() }).and_then(|()| {
                if granted.as_bool() {
                    Ok(())
                } else {
                    Err(AppError::Worker(
                        "Notification permission is disabled in System Settings.".into(),
                    ))
                }
            });
            let _ = tx.try_send(result);
        });
        UNUserNotificationCenter::currentNotificationCenter()
            .requestAuthorizationWithOptions_completionHandler(
                UNAuthorizationOptions::Alert | UNAuthorizationOptions::Sound,
                &callback,
            );
        rx
    });
    wait_for_system(completion).await
}

pub(super) async fn send(title: &str, body: &str) -> AppResult<()> {
    let completion = autoreleasepool(|_| {
        let content = UNMutableNotificationContent::new();
        content.setTitle(&NSString::from_str(title));
        content.setBody(&NSString::from_str(body));
        content.setSound(Some(&UNNotificationSound::defaultSound()));
        let request = UNNotificationRequest::requestWithIdentifier_content_trigger(
            &NSUUID::UUID().UUIDString(),
            &content,
            None,
        );
        let (tx, rx) = channel(1);
        let callback = RcBlock::new(move |error: *mut NSError| {
            // SAFETY: Apple supplies an optional borrowed NSError for the duration of the callback.
            let _ = tx.try_send(system_result(unsafe { error.as_ref() }));
        });
        UNUserNotificationCenter::currentNotificationCenter()
            .addNotificationRequest_withCompletionHandler(&request, Some(&callback));
        rx
    });
    wait_for_system(completion).await
}

fn system_result(error: Option<&NSError>) -> AppResult<()> {
    match error {
        Some(error) => Err(AppError::Worker(error.localizedDescription().to_string())),
        None => Ok(()),
    }
}

async fn wait_for_system(mut completion: Receiver<AppResult<()>>) -> AppResult<()> {
    completion.recv().await.ok_or_else(|| {
        AppError::Worker("The system notification request ended without a result.".into())
    })?
}
