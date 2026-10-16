package com.codestick.uiprobe;

import android.app.Activity;
import android.app.Instrumentation;
import android.app.UiAutomation;
import android.graphics.Rect;
import android.hardware.display.DisplayManager;
import android.os.Bundle;
import android.util.Base64;
import android.util.Xml;
import android.view.Display;
import android.view.accessibility.AccessibilityNodeInfo;
import org.xmlpull.v1.XmlSerializer;

import java.io.StringWriter;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.concurrent.atomic.AtomicBoolean;

/** Read-only diagnostic. No activity, target-app API, input, idle wait or grants. */
public final class UiProbe extends Instrumentation {
    private static final String PROTOCOL = "codestick-no-idle-accessibility-v1";
    private static final String TARGET = "com.codestick.aidrive";
    private static final int MAX_NODES = 256;
    private static final int MAX_DEPTH = 32;
    private static final int MAX_ATTRIBUTE_CHARS = 2048;
    private static final int MAX_TOTAL_ATTRIBUTE_CHARS = 65536;
    private static final int MAX_XML_BYTES = 131072;
    private static final int MAX_CHILD_QUERIES = 512;
    private static final long WATCHDOG_MS = 30000;
    private static final long ROOT_BUDGET_MS = 5000;
    private static final long ROOT_POLL_MS = 150;
    private final AtomicBoolean finished = new AtomicBoolean(false);
    private String nonce = "";
    private int count = 0;
    private int childQueries = 0;
    private int remainingChars = MAX_TOTAL_ATTRIBUTE_CHARS;
    private boolean truncated = false;
    private int rootAttempts = 0;
    private int rootNullReads = 0;
    private long rootElapsedMs = 0;

    @Override public void onCreate(Bundle arguments) {
        String supplied = arguments == null ? null : arguments.getString("probe_nonce");
        if (supplied == null || !supplied.matches("[a-f0-9]{32}")) {
            finishResult("invalid_nonce", "A fresh 32-character hex probe_nonce is required", null, "");
            return;
        }
        nonce = supplied;
        start();
    }

    @Override public void onStart() {
        Thread watchdog = new Thread(() -> {
            try {
                Thread.sleep(WATCHDOG_MS);
                finishResult("timeout", "Accessibility query did not complete within 30 seconds", null, "");
            } catch (InterruptedException ignored) { }
        }, "UiProbe-watchdog");
        watchdog.setDaemon(true);
        watchdog.start();
        AccessibilityNodeInfo root = null;
        try {
            final UiAutomation automation = getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
            // Deliberately no waitForIdle, executeAndWaitForEvent or UI input.
            RootPoller.Result<AccessibilityNodeInfo> acquired = RootPoller.acquire(
                new RootPoller.Reader<AccessibilityNodeInfo>() {
                    public AccessibilityNodeInfo read() { return automation.getRootInActiveWindow(); }
                    public String packageName(AccessibilityNodeInfo node) { return string(node.getPackageName()); }
                    public void release(AccessibilityNodeInfo node) { node.recycle(); }
                }, TARGET, ROOT_BUDGET_MS, ROOT_POLL_MS);
            rootAttempts = acquired.attempts;
            rootNullReads = acquired.nullReads;
            rootElapsedMs = acquired.elapsedMs;
            if (!"ok".equals(acquired.status)) {
                finishResult(acquired.status, "Bounded root acquisition did not return the target window", null, acquired.packageName);
                return;
            }
            root = acquired.root;
            String activePackage = string(root.getPackageName());
            if (!TARGET.equals(activePackage)) {
                finishResult("wrong_active_package", "The active accessibility window is not AI Drive", null, activePackage);
                return;
            }
            int rotation = -1;
            DisplayManager manager = (DisplayManager) getContext().getSystemService(android.content.Context.DISPLAY_SERVICE);
            Display display = manager == null ? null : manager.getDisplay(Display.DEFAULT_DISPLAY);
            if (display != null) rotation = display.getRotation();
            StringWriter writer = new StringWriter();
            XmlSerializer xml = Xml.newSerializer();
            xml.setOutput(writer);
            xml.startDocument("UTF-8", true);
            xml.startTag("", "hierarchy");
            xml.attribute("", "rotation", Integer.toString(rotation));
            xml.attribute("", "probe-method", "accessibility-no-idle");
            node(xml, root, 0, 0);
            xml.endTag("", "hierarchy");
            xml.endDocument();
            xml.flush();
            byte[] bytes = writer.toString().getBytes(StandardCharsets.UTF_8);
            if (bytes.length > MAX_XML_BYTES) {
                finishResult("xml_limit", "Accessibility XML exceeded the fixed byte cap", null, activePackage);
                return;
            }
            finishResult(truncated ? "truncated" : "ok", "", bytes, activePackage);
        } catch (Throwable error) {
            // Return only a bounded diagnostic, never substitute cached UI.
            finishResult("query_error", error.getClass().getSimpleName() + ": " + string(error.getMessage()), null, "");
        } finally {
            if (root != null) root.recycle();
            watchdog.interrupt();
        }
    }

    private void node(XmlSerializer xml, AccessibilityNodeInfo current, int index, int depth) throws Exception {
        if (finished.get()) throw new IllegalStateException("Probe already finished");
        if (count >= MAX_NODES || depth > MAX_DEPTH) { truncated = true; return; }
        count++;
        xml.startTag("", "node");
        attribute(xml, "index", Integer.toString(index));
        attribute(xml, "text", string(current.getText()));
        attribute(xml, "resource-id", string(current.getViewIdResourceName()));
        attribute(xml, "class", string(current.getClassName()));
        attribute(xml, "package", string(current.getPackageName()));
        attribute(xml, "content-desc", string(current.getContentDescription()));
        attribute(xml, "checkable", Boolean.toString(current.isCheckable()));
        attribute(xml, "checked", Boolean.toString(current.isChecked()));
        attribute(xml, "clickable", Boolean.toString(current.isClickable()));
        attribute(xml, "enabled", Boolean.toString(current.isEnabled()));
        attribute(xml, "focusable", Boolean.toString(current.isFocusable()));
        attribute(xml, "focused", Boolean.toString(current.isFocused()));
        attribute(xml, "scrollable", Boolean.toString(current.isScrollable()));
        attribute(xml, "long-clickable", Boolean.toString(current.isLongClickable()));
        attribute(xml, "password", Boolean.toString(current.isPassword()));
        attribute(xml, "selected", Boolean.toString(current.isSelected()));
        attribute(xml, "visible-to-user", Boolean.toString(current.isVisibleToUser()));
        Rect bounds = new Rect();
        current.getBoundsInScreen(bounds);
        attribute(xml, "bounds", "[" + bounds.left + "," + bounds.top + "][" + bounds.right + "," + bounds.bottom + "]");
        int children = current.getChildCount();
        for (int childIndex = 0; childIndex < children; childIndex++) {
            if (count >= MAX_NODES) { truncated = true; break; }
            if (childQueries >= MAX_CHILD_QUERIES) { truncated = true; break; }
            childQueries++;
            AccessibilityNodeInfo child = current.getChild(childIndex);
            if (child == null) continue;
            try {
                if (child.isVisibleToUser()) node(xml, child, childIndex, depth + 1);
            } finally { child.recycle(); }
        }
        xml.endTag("", "node");
    }

    private void attribute(XmlSerializer xml, String name, String value) throws Exception {
        int allowed = Math.min(MAX_ATTRIBUTE_CHARS, remainingChars);
        if (value.length() > allowed) { value = value.substring(0, allowed); truncated = true; }
        remainingChars -= value.length();
        // Standard XML escaping is delegated to XmlSerializer. Replace only
        // characters forbidden by XML 1.0, as UiAutomator's dumper also does.
        StringBuilder clean = new StringBuilder();
        for (int offset = 0; offset < value.length();) {
            int code = value.codePointAt(offset);
            offset += Character.charCount(code);
            if (code == 9 || code == 10 || code == 13 || (code >= 32 && code <= 0xD7FF)
                    || (code >= 0xE000 && code <= 0xFFFD) || (code >= 0x10000 && code <= 0x10FFFF))
                clean.appendCodePoint(code);
            else { clean.append('\uFFFD'); truncated = true; }
        }
        xml.attribute("", name, clean.toString());
    }

    private void finishResult(String status, String error, byte[] xml, String activePackage) {
        if (!finished.compareAndSet(false, true)) return;
        Bundle result = new Bundle();
        result.putString("probe_protocol", PROTOCOL);
        result.putString("probe_nonce", nonce);
        result.putString("probe_status", status);
        error = error.replace('\n', ' ').replace('\r', ' ');
        result.putString("probe_error", error.length() > 400 ? error.substring(0, 400) : error);
        result.putString("probe_active_package", activePackage);
        result.putString("probe_nodes", Integer.toString(count));
        result.putString("probe_truncated", Boolean.toString(truncated));
        result.putString("probe_wait_for_idle", "false");
        result.putString("probe_version", "2");
        result.putString("probe_root_attempts", Integer.toString(rootAttempts));
        result.putString("probe_root_null_reads", Integer.toString(rootNullReads));
        result.putString("probe_root_elapsed_ms", Long.toString(rootElapsedMs));
        result.putString("probe_root_budget_ms", Long.toString(ROOT_BUDGET_MS));
        result.putString("probe_root_poll_ms", Long.toString(ROOT_POLL_MS));
        if (xml != null) {
            result.putString("probe_xml_base64", Base64.encodeToString(xml, Base64.NO_WRAP));
            try {
                byte[] digest = MessageDigest.getInstance("SHA-256").digest(xml);
                StringBuilder hex = new StringBuilder();
                for (byte value : digest) hex.append(String.format(java.util.Locale.ROOT, "%02x", value & 255));
                result.putString("probe_xml_sha256", hex.toString());
            } catch (Exception errorHashing) {
                result.putString("probe_status", "hash_error");
            }
        }
        finish("ok".equals(result.getString("probe_status")) ? Activity.RESULT_OK : Activity.RESULT_CANCELED, result);
    }

    private static String string(CharSequence value) { return value == null ? "" : value.toString(); }
}
