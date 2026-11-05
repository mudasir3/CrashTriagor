package com.example.triagedemo

import android.app.AlertDialog
import android.graphics.Bitmap
import android.os.Handler
import android.os.Looper
import android.os.StrictMode
import androidx.activity.result.contract.ActivityResultContracts
import androidx.fragment.app.DialogFragment
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.launch
import java.io.File
import java.net.URL

/**
 * Each scenario reproduces one golden-set crash family on purpose.
 * Capture:  adb logcat -d -b crash > golden/crashes/<new_id>.txt
 * ANRs:     adb bugreport, then copy the main-thread section from FS/data/anr/
 * Label the capture in golden/labels.jsonl with "split": "test".
 */
data class Scenario(val goldenId: String, val title: String, val howTo: String, val run: (MainActivity) -> Unit)

private lateinit var neverAssigned: List<String>
private val sharedLock = Any()

class CouponDialog : DialogFragment()

val scenarios = listOf(
    Scenario("c01", "Fragment state loss", "Tap, then press Home within 2 s") { a ->
        a.lifecycleScope.launch {
            delay(2000)
            CouponDialog().show(a.supportFragmentManager, "coupon") // after onSaveInstanceState
        }
    },
    Scenario("c04", "Late registerForActivityResult", "Tap once") { a ->
        a.registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {}
    },
    Scenario("c05", "lateinit not initialised", "Tap once") { _ ->
        println(neverAssigned.size)
    },
    Scenario("c06", "Platform-type NPE", "Tap once") { _ ->
        val lang: String = LegacyPrefs.getLanguage() // NPE: must not be null
        println(lang)
    },
    Scenario("c08", "Network on main thread", "Tap once (needs INTERNET)") { _ ->
        URL("https://example.com").readText()
    },
    Scenario("c09", "StrictMode disk read (no crash)", "Tap, then read logcat tag StrictMode") { a ->
        StrictMode.setThreadPolicy(StrictMode.ThreadPolicy.Builder().detectDiskReads().penaltyLog().build())
        File(a.filesDir, "theme.txt").apply { if (!exists()) writeText("dark") }.readText()
    },
    Scenario("c10", "ConcurrentModificationException", "Tap once") { _ ->
        val items = mutableListOf(1, 2, 3)
        for (i in items) if (i == 2) items.add(4)
    },
    Scenario("c11", "Uncaught exception in launch", "Tap once") { a ->
        a.lifecycleScope.launch(Dispatchers.IO) { URL("https://does-not-exist.invalid").readText() }
    },
    Scenario("c12", "Flow invariant violated", "Tap once") { a ->
        a.lifecycleScope.launch {
            flow { coroutineScope { launch(Dispatchers.Default) { emit(1) } } }.collect { }
        }
    },
    Scenario("c13", "Bitmap OOM", "Tap once") { _ ->
        val keep = mutableListOf<Bitmap>()
        repeat(100) { keep += Bitmap.createBitmap(4000, 4000, Bitmap.Config.ARGB_8888) }
    },
    Scenario("c15", "TransactionTooLarge", "Tap, then press Home") { a ->
        a.oversizedState = true // MainActivity puts 2 MB into onSaveInstanceState
    },
    Scenario("c17", "BadTokenException", "Tap once") { a ->
        a.finish()
        Handler(Looper.getMainLooper()).postDelayed({ AlertDialog.Builder(a).setMessage("Paid").show() }, 500)
    },
    Scenario("c19", "Duplicate LazyColumn key", "Tap to open the list") { a ->
        a.showDuplicateKeys = true
    },
    Scenario("c25", "ANR: main thread waits on a lock", "Tap, then tap the screen a few times") { _ ->
        Thread {
            synchronized(sharedLock) { Thread.sleep(15_000) } // holder does slow work under the lock
        }.start()
        Thread.sleep(100)
        synchronized(sharedLock) { } // main thread blocks
    },
    Scenario("c29", "Class removed (reflection)", "Tap once") { _ ->
        Class.forName("com.example.triagedemo.export.plugins.PdfExporter")
    },
)
