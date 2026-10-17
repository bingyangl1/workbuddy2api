package io.github.bingyangl1.workbuddy2api;

import android.Manifest;
import android.app.Activity;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.*;
import android.view.*;
import android.webkit.*;
import android.widget.*;
import org.json.JSONObject;
import com.chaquo.python.Python;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

public class MainActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private WebView web;
    private TextView status;
    private FrameLayout boot;
    private TextView bootTitle;
    private ProgressBar progress;
    private Button details;
    private ValueCallback<Uri[]> upload;
    private String loadedOrigin;
    private int loadedRevision;
    private boolean destroyed;
    private volatile boolean bridgeTrusted;
    private Button restart;
    private volatile String exportRequest;
    private byte[] exportBytes;
    private volatile boolean exportWriting;
    /**
     * 面板落点：`next` = 新面板（/panel），`legacy` = 旧面板（/admin）。
     *
     * 与桌面端语义一致（`desktop/app.py::_landing_url`）：
     *   next   → /panel?desktop=1&android=1&mobile=1
     *   legacy → /admin?desktop=1&android=1
     *
     * **默认 next**：与桌面版对齐，且新面板本身已有移动端适配
     * （汉堡菜单 + portal 抽屉 + 375px 响应式，都有测试守着）。
     *
     * 旧面板仍然打包在 APK 里 —— 新面板万一出问题，改这个常量即可回退。
     */
    private static final String DEFAULT_PANEL = "next";

    /** 当前要加载的面板路径（含 query）。 */
    private String landingPath() {
        boolean next = !"legacy".equalsIgnoreCase(DEFAULT_PANEL);
        if (!next) return "/admin?desktop=1&android=1";
        // `mobile=1` 让新面板知道自己在手机上（配合窄屏布局与触控优化）
        return "/panel?desktop=1&android=1&mobile=1";
    }

    private final Runnable waitForServer = new Runnable() {
        @Override public void run() {
            if (destroyed) return;
            String origin = LocalApiService.origin;
            if (origin != null && !LocalApiService.starting &&
                    (!origin.equals(loadedOrigin) || loadedRevision != LocalApiService.revision)) {
                loadedOrigin = origin;
                loadedRevision = LocalApiService.revision;
                web.loadUrl(origin + landingPath());
            } else if (origin == null) {
                boot.setVisibility(View.VISIBLE);
                boolean failed = LocalApiService.error != null;
                bootTitle.setText(failed ? "服务启动失败" : LocalApiService.starting ? "正在打开工作台" : "本地服务已停止");
                status.setText(failed ? "请重试启动。若仍无法打开，可查看错误详情。" : LocalApiService.starting ? "Python 和项目依赖已内置\n正在启动手机本地 API 服务…" : "点击下方按钮，继续使用手机本地 API。");
                progress.setVisibility(LocalApiService.starting ? View.VISIBLE : View.GONE);
                restart.setVisibility(LocalApiService.starting ? View.GONE : View.VISIBLE);
                details.setVisibility(failed ? View.VISIBLE : View.GONE);
                if (loadedOrigin != null) { loadedOrigin = null; web.loadUrl("about:blank"); }
            }
            handler.postDelayed(this, 500);
        }
    };

    @Override public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().setStatusBarColor(Color.rgb(245, 246, 248));
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR);
        FrameLayout layout = new FrameLayout(this);
        layout.setFitsSystemWindows(true);
        web = new WebView(this);
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        layout.addView(web, new FrameLayout.LayoutParams(-1, -1));
        boot = new FrameLayout(this);
        boot.setBackgroundColor(Color.rgb(245, 246, 248));
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setGravity(Gravity.CENTER_HORIZONTAL);
        card.setPadding(dp(28), dp(32), dp(28), dp(28));
        GradientDrawable surface = new GradientDrawable();
        surface.setColor(Color.WHITE); surface.setCornerRadius(dp(24));
        card.setBackground(surface); card.setElevation(dp(4));
        int width = Math.min(dp(340), getResources().getDisplayMetrics().widthPixels - dp(40));
        FrameLayout.LayoutParams cardParams = new FrameLayout.LayoutParams(width, -2, Gravity.CENTER);
        boot.addView(card, cardParams);
        ImageView logo = new ImageView(this);
        logo.setImageResource(R.drawable.app_icon);
        card.addView(logo, new LinearLayout.LayoutParams(dp(68), dp(68)));
        TextView brand = new TextView(this);
        brand.setText("WorkBuddy2API"); brand.setTextSize(24); brand.setTypeface(null, Typeface.BOLD);
        brand.setTextColor(Color.rgb(22, 24, 29)); brand.setGravity(Gravity.CENTER);
        brand.setPadding(0, dp(18), 0, dp(20)); card.addView(brand);
        bootTitle = new TextView(this);
        bootTitle.setText("正在打开工作台"); bootTitle.setTextSize(16);
        bootTitle.setTextColor(Color.rgb(22, 24, 29)); card.addView(bootTitle);
        status = new TextView(this);
        status.setText("Python 和项目依赖已内置\n正在启动手机本地 API 服务…");
        status.setTextSize(13); status.setGravity(Gravity.CENTER);
        status.setTextColor(Color.rgb(101, 105, 112)); status.setLineSpacing(dp(5), 1);
        status.setPadding(0, dp(12), 0, dp(20)); card.addView(status);
        progress = new ProgressBar(this);
        progress.setIndeterminateTintList(android.content.res.ColorStateList.valueOf(Color.rgb(20, 134, 101)));
        card.addView(progress, new LinearLayout.LayoutParams(dp(28), dp(28)));
        restart = new Button(this);
        restart.setText("启动服务");
        restart.setTextColor(Color.WHITE);
        GradientDrawable action = new GradientDrawable();
        action.setColor(Color.rgb(20, 134, 101)); action.setCornerRadius(dp(12));
        restart.setBackground(action); restart.setVisibility(View.GONE);
        restart.setOnClickListener(view -> startLocalService(true));
        card.addView(restart, new LinearLayout.LayoutParams(-1, dp(48)));
        details = new Button(this);
        details.setText("查看错误详情"); details.setTextSize(12); details.setTextColor(Color.rgb(101, 105, 112));
        details.setBackgroundColor(Color.TRANSPARENT); details.setVisibility(View.GONE);
        details.setOnClickListener(view -> new android.app.AlertDialog.Builder(this).setTitle("启动错误")
            .setMessage(LocalApiService.error).setPositiveButton("关闭", null).show());
        card.addView(details);
        layout.addView(boot, new FrameLayout.LayoutParams(-1, -1));
        setContentView(layout);
        web.getSettings().setJavaScriptEnabled(true);
        web.getSettings().setDomStorageEnabled(true);
        // Local assets change with APK upgrades, even while the origin stays fixed.
        web.getSettings().setCacheMode(WebSettings.LOAD_NO_CACHE);
        web.getSettings().setAllowFileAccess(false);
        web.getSettings().setAllowContentAccess(true);
        web.getSettings().setSupportMultipleWindows(true);
        web.getSettings().setJavaScriptCanOpenWindowsAutomatically(false);
        web.getSettings().setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        web.addJavascriptInterface(new MobileBridge(), "WBAndroid");
        web.setWebViewClient(new WebViewClient() {
            @Override public void onPageStarted(WebView view, String url, android.graphics.Bitmap icon) {
                bridgeTrusted = isAdmin(url);
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return route(request.getUrl());
            }
            @Override public void onPageFinished(WebView view, String url) {
                if (isAdmin(url) && LocalApiService.token != null) {
                    // ── 免密登录：**直接把 token 写进浏览器存储** ──────
                    //
                    // 为什么不只调 `WBMobile.initialize()`：
                    //
                    //   `mobile.js`（含 WBMobile）只被注入到**旧面板**的
                    //   `admin/static/index.html`。新面板是 Next.js 静态产物，
                    //   **根本不加载 mobile.js** —— 于是 `window.WBMobile` 不存在，
                    //   `&&` 短路，token 永远写不进去，新面板停在登录页。
                    //
                    // ## 三个存储点，缺一不可
                    //
                    //   旧面板 admin/static/index.html → localStorage 'wb_admin_token'
                    //   新面板 web/lib/api.ts           → **sessionStorage** 'wb-admin-token'
                    //      （`readToken()` 只读 sessionStorage，写 localStorage 读不到！）
                    //   兜底：localStorage 也写一份 'wb-admin-token'，
                    //      供将来可能改成持久化的实现使用
                    String t = JSONObject.quote(LocalApiService.token);
                    String inject = "(function(){try{"
                        + "var v=" + t + ";"
                        + "localStorage.setItem('wb_admin_token',v);"
                        + "localStorage.setItem('wb-admin-token',v);"
                        + "sessionStorage.setItem('wb-admin-token',v);"
                        + "}catch(e){}"
                        + "try{if(window.WBMobile&&WBMobile.initialize){"
                        + "WBMobile.initialize(" + t + ");}}catch(e){}"
                        // 新面板：token 是「页面加载完之后」才写进来的，
                        // 此时 React 早已判定未登录并跳到了 /login/。
                        // 补一次跳转 —— 只在停在 login 页或根路径时做，
                        // 避免打断用户已经在操作的页面。
                        //
                        // 用 sessionStorage 标记防循环：跳转后 `onPageFinished`
                        // 会再次触发，若条件仍成立就会无限刷新。
                        + "try{"
                        + "var FLAG='wb_auto_entered';"
                        + "if(!sessionStorage.getItem(FLAG)){"
                        + "  var p=location.pathname||'';"
                        + "  var need=(p.indexOf('/login')>=0||p===''||p==='/'||"
                        + "            p==='/panel'||p==='/panel/');"
                        + "  if(need){sessionStorage.setItem(FLAG,'1');"
                        + "    location.replace('/panel/dashboard/');return;}"
                        + "}"
                        + "}catch(e){}"
                        + "})();";
                    web.evaluateJavascript(inject, null);
                    boot.setVisibility(View.GONE);
                }
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    bootTitle.setText("工作台连接失败"); status.setText("请点击重试重新连接本地服务。");
                    progress.setVisibility(View.GONE); restart.setText("重新连接"); restart.setVisibility(View.VISIBLE);
                    boot.setVisibility(View.VISIBLE);
                }
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onCreateWindow(WebView view, boolean dialog, boolean userGesture, Message message) {
                if (!userGesture) return false;
                WebView popup = new WebView(MainActivity.this);
                popup.setWebViewClient(new WebViewClient() {
                    private boolean opened;
                    private void open(String url) { if (!opened) { opened = true; external(Uri.parse(url)); popup.destroy(); } }
                    @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) { open(r.getUrl().toString()); return true; }
                    @Override public void onPageStarted(WebView v, String url, android.graphics.Bitmap icon) { if (!"about:blank".equals(url)) open(url); }
                });
                ((WebView.WebViewTransport) message.obj).setWebView(popup); message.sendToTarget(); return true;
            }
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (upload != null) upload.onReceiveValue(null);
                upload = callback;
                Intent intent = new Intent(Intent.ACTION_GET_CONTENT).setType("*/*").addCategory(Intent.CATEGORY_OPENABLE);
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE);
                try { startActivityForResult(Intent.createChooser(intent, "选择账号备份"), 10); return true; }
                catch (ActivityNotFoundException e) { upload.onReceiveValue(null); upload = null; return false; }
            }
        });
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 11);
        startLocalService();
        handler.post(waitForServer);
    }

    private void startLocalService() {
        startLocalService(false);
    }

    private void startLocalService(boolean recover) {
        Intent service = new Intent(this, LocalApiService.class);
        if (recover) service.setAction("restart");
        try {
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(service); else startService(service);
        } catch (RuntimeException e) {
            LocalApiService.starting = false;
            LocalApiService.error = "系统未能启动前台服务：" + e.getMessage();
        }
    }

    private class MobileBridge {
        @JavascriptInterface public String sessionToken() {
            return bridgeTrusted && LocalApiService.token != null ? LocalApiService.token : "";
        }
        @JavascriptInterface public synchronized boolean saveText(String request, String filename, String text, String mime) {
            if (!bridgeTrusted || destroyed || request == null || !request.matches("[0-9]{1,10}") || exportRequest != null || exportWriting || text == null) return false;
            byte[] bytes = text.getBytes(StandardCharsets.UTF_8);
            if (bytes.length > 32 * 1024 * 1024) return false;
            String safe = filename == null ? "workbuddy-export.info" : filename.replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]", "_");
            if (safe.length() > 120) safe = safe.substring(0, 120);
            if (!safe.endsWith(".info") && !safe.endsWith(".json") && !safe.endsWith(".csv")) safe += ".info";
            exportRequest = request;
            exportBytes = bytes;
            final String title = safe;
            final String type = "text/csv".equals(mime) ? "text/csv" : "application/json";
            handler.post(() -> {
                if (destroyed) return;
                Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                    .setType(type).putExtra(Intent.EXTRA_TITLE, title);
                try { startActivityForResult(intent, 12); }
                catch (ActivityNotFoundException e) { finishExport(false, "找不到文件保存应用"); }
            });
            return true;
        }
        @JavascriptInterface public String status() {
            if (!bridgeTrusted) return "{}";
            JSONObject data = new JSONObject();
            try {
                android.app.NotificationManager manager = getSystemService(android.app.NotificationManager.class);
                boolean notifications = manager.areNotificationsEnabled();
                if (Build.VERSION.SDK_INT >= 26) {
                    android.app.NotificationChannel channel = manager.getNotificationChannel("local-api");
                    notifications &= channel != null && channel.getImportance() != android.app.NotificationManager.IMPORTANCE_NONE;
                }
                PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
                data.put("registered", LocalApiService.registered);
                data.put("starting", LocalApiService.starting);
                data.put("last_healthy", LocalApiService.lastHealthy);
                data.put("notifications", notifications);
                data.put("battery_unrestricted", power.isIgnoringBatteryOptimizations(getPackageName()));
                android.app.ActivityManager activity = (android.app.ActivityManager) getSystemService(ACTIVITY_SERVICE);
                data.put("background_restricted", Build.VERSION.SDK_INT >= 28 && activity.isBackgroundRestricted());
                data.put("power_save", power.isPowerSaveMode());
                data.put("wake_lock", LocalApiService.wakeLockHeld());
                data.put("origin", LocalApiService.origin);
                data.put("error", LocalApiService.error);
            } catch (Exception ignored) { }
            return data.toString();
        }
        @JavascriptInterface public void restartService() {
            if (bridgeTrusted) handler.post(() -> { if (!destroyed) startLocalService(true); });
        }
        @JavascriptInterface public void openSystemSettings(String section) {
            if (!bridgeTrusted) return;
            handler.post(() -> {
                if (destroyed) return;
                Intent intent;
                if ("notifications".equals(section) && Build.VERSION.SDK_INT >= 26) {
                    intent = new Intent(android.provider.Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                        .putExtra(android.provider.Settings.EXTRA_APP_PACKAGE, getPackageName());
                } else if ("battery".equals(section)) {
                    PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
                    if (power.isIgnoringBatteryOptimizations(getPackageName())) {
                        intent = new Intent(android.provider.Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS);
                    } else {
                        intent = new Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                                            Uri.parse("package:" + getPackageName()));
                    }
                } else {
                    intent = new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                        Uri.parse("package:" + getPackageName()));
                }
                try { startActivity(intent); }
                catch (RuntimeException e) {
                    try { startActivity(new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                                  Uri.parse("package:" + getPackageName()))); }
                    catch (RuntimeException ignored) { Toast.makeText(MainActivity.this, "无法打开系统设置", Toast.LENGTH_SHORT).show(); }
                }
            });
        }
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    /**
     * 这个 URL 是否属于「本站面板」（需要注入 token / 允许在 WebView 内导航）。
     *
     * ⚠️ **必须同时认 `/panel`**（新面板）。
     *
     * 原实现只认 `"/admin".equals(uri.getPath())` —— 那是单面板时代的写法。
     * 一旦落点改到新面板，`onPageFinished` 里的 `isAdmin(url)` 就恒为 false，
     * 于是 `WBMobile.initialize(token)` **永不执行**，界面会永久停在
     * 「正在打开工作台」引导层上（真机上排查了很久才定位）。
     *
     * 现在同时接受：
     *   /admin           旧面板
     *   /admin/...       旧面板子路径
     *   /panel           新面板入口
     *   /panel/...       新面板各页（/panel/models/ 等）
     */
    private boolean isAdmin(String url) {
        if (loadedOrigin == null || url == null) return false;
        Uri uri = Uri.parse(url);
        if (!"http".equals(uri.getScheme())) return false;
        if (!"127.0.0.1".equals(uri.getHost())) return false;
        if (uri.getPort() != Uri.parse(loadedOrigin).getPort()) return false;
        String path = uri.getPath();
        if (path == null) return false;
        return path.equals("/admin") || path.startsWith("/admin/")
            || path.equals("/panel") || path.startsWith("/panel/");
    }
    private boolean route(Uri uri) {
        if (isAdmin(uri.toString())) return false;
        external(uri); return true;
    }
    private void external(Uri uri) {
        if (!"https".equals(uri.getScheme()) && !"http".equals(uri.getScheme())) return;
        try { startActivity(new Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE)); }
        catch (ActivityNotFoundException e) { Toast.makeText(this, "请先安装浏览器", Toast.LENGTH_SHORT).show(); }
    }
    @Override protected void onResume() {
        super.onResume();
        // Re-register the foreground service even when Python left an old origin in memory.
        if (web != null) startLocalService();
        if (web != null) web.evaluateJavascript("window.dispatchEvent(new Event('wb-shell-resume'))", null);
        if (LocalApiService.origin != null && Python.isStarted()) new Thread(() -> {
            try {
                String refreshed = Python.getInstance().getModule("android_runtime").callAttr("session_token").toString();
                if (!refreshed.isEmpty()) {
                    LocalApiService.token = refreshed;
                    handler.post(() -> {
                        if (!destroyed && isAdmin(web.getUrl())) {
                            // 与 onPageFinished 同一套逻辑。
                            // 关键：新面板 `readToken()` 读的是 **sessionStorage**，
                            // 只写 localStorage 是读不到的（真机踩过）。
                            String t = JSONObject.quote(refreshed);
                            String js = "(function(){try{"
                                + "var v=" + t + ";"
                                + "localStorage.setItem('wb_admin_token',v);"
                                + "localStorage.setItem('wb-admin-token',v);"
                                + "sessionStorage.setItem('wb-admin-token',v);"
                                + "}catch(e){}"
                                + "try{if(window.WBMobile&&WBMobile.initialize){"
                                + "WBMobile.initialize(" + t + ");}}catch(e){}"
                                + "})();";
                            web.evaluateJavascript(js, null);
                        }
                    });
                }
            } catch (Exception ignored) { }
        }, "workbuddy-session-refresh").start();
    }
    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == 10 && upload != null) {
            Uri[] selected = null;
            if (result == RESULT_OK && data != null && data.getClipData() != null) {
                ClipData clips = data.getClipData();
                selected = new Uri[clips.getItemCount()];
                for (int i = 0; i < clips.getItemCount(); i++) selected[i] = clips.getItemAt(i).getUri();
            } else selected = WebChromeClient.FileChooserParams.parseResult(result, data);
            upload.onReceiveValue(selected); upload = null;
        }
        if (request == 12 && exportRequest != null) {
            if (result != RESULT_OK || data == null || data.getData() == null) {
                finishExport(false, "已取消保存");
                return;
            }
            final Uri target = data.getData();
            final byte[] content = exportBytes;
            exportWriting = true;
            new Thread(() -> {
                boolean success = false;
                try (OutputStream output = getContentResolver().openOutputStream(target, "wt")) {
                    if (output == null || content == null) throw new java.io.IOException("No writable document");
                    output.write(content);
                    output.flush();
                    success = true;
                } catch (Exception e) {
                    success = false;
                    android.util.Log.e("WorkBuddyExport", "Document write failed: " + e.getClass().getSimpleName());
                }
                final boolean saved = success;
                handler.post(() -> finishExport(saved, saved ? "文件已保存" : "文件写入失败，请更换保存位置"));
            }, "workbuddy-file-export").start();
        }
    }
    private void finishExport(boolean success, String message) {
        String id = exportRequest;
        exportRequest = null; exportBytes = null; exportWriting = false;
        if (!destroyed && bridgeTrusted && id != null)
            web.evaluateJavascript("window.WBMobile && WBMobile.saveResult(" + JSONObject.quote(id) + "," + success + "," + JSONObject.quote(message) + ")", null);
    }
    @Override public void onBackPressed() {
        if (web != null && isAdmin(web.getUrl())) web.evaluateJavascript("if(document.querySelector('dialog[open]'))document.querySelector('dialog[open]').close();else if(!document.getElementById('modal').classList.contains('hidden'))closeModal();else closeSideMenu();", null);
        else super.onBackPressed();
    }
    @Override protected void onDestroy() {
        destroyed = true; handler.removeCallbacksAndMessages(null);
        bridgeTrusted = false;
        if (upload != null) upload.onReceiveValue(null);
        if (web != null) web.destroy();
        super.onDestroy();
    }
}
