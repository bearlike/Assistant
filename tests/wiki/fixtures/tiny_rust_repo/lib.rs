use std::collections::HashMap;

pub struct Cache {
    items: HashMap<String, String>,
}

pub trait Loader {
    fn load(&self, path: &str) -> Vec<u8>;
}

impl Cache {
    pub fn new() -> Self {
        Cache { items: HashMap::new() }
    }

    pub fn get(&self, k: &str) -> Option<&String> {
        self.items.get(k)
    }
}

fn helper() {
    println!("hi");
}
