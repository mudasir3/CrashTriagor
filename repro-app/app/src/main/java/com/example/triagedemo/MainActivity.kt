package com.example.triagedemo

import android.os.Bundle
import androidx.activity.compose.setContent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.fragment.app.FragmentActivity

/** FragmentActivity (not just ComponentActivity) so the DialogFragment scenario works. */
class MainActivity : FragmentActivity() {
    var oversizedState = false
    var showDuplicateKeys by mutableStateOf(false)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                if (showDuplicateKeys) {
                    LazyColumn {
                        items(listOf(42, 42), key = { it }) { Text("Post $it") } // same key twice
                    }
                } else {
                    LazyColumn {
                        items(scenarios, key = { it.goldenId }) { s ->
                            Column(Modifier.clickable { s.run(this@MainActivity) }.padding(16.dp)) {
                                Text("${s.goldenId}  ${s.title}", style = MaterialTheme.typography.titleMedium)
                                Text(s.howTo, style = MaterialTheme.typography.bodySmall)
                            }
                            HorizontalDivider()
                        }
                    }
                }
            }
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        if (oversizedState) outState.putByteArray("cache", ByteArray(2 * 1024 * 1024))
    }
}
