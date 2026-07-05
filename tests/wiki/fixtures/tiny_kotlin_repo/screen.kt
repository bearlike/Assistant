package sample.ui

import sample.models.Greeter
import sample.models.Mode
import sample.models.Registry

class HomeScreen(private val greeter: Greeter) : Greeter {
    val title: String = "Home"
    private var mode: Mode = Mode.LIGHT

    override fun greet(name: String): String = "hi $name"

    fun boot(): String = Registry.bootstrap()

    companion object {
        fun create(greeter: Greeter): HomeScreen = HomeScreen(greeter)
    }
}

fun launch(greeter: Greeter): String {
    val screen = HomeScreen.create(greeter)
    return screen.greet("world")
}
