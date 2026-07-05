package sample.ui

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier

@Composable
fun Greeting(name: String, modifier: Modifier = Modifier) {
    val handler = { println(name) }
    handler()
}
