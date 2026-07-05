package sample;

import java.util.List;

class HomeScreen extends BaseScreen implements Greeter {
    private final Greeter greeter;
    public String title = "Home";

    public HomeScreen(Greeter greeter) {
        this.greeter = greeter;
    }

    @Override
    public String greet(String name) {
        return "hi " + name;
    }

    public static HomeScreen create() {
        return new HomeScreen(Singleton.instance());
    }
}

class Singleton {
    static Singleton instance() {
        return new Singleton();
    }

    void ping() {
        System.out.println("pong");
    }
}
