import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("LogReader")


def follow_logs(file_path):
    try:
        with open(file_path, "r") as file:

            for line in file:
                logger.info(line.strip())

            
            while True:
                line = file.readline()
                if not line:
                    time.sleep(0.5)   
                    continue
                logger.info(line.strip())
    except FileNotFoundError:
        logger.error(f"File '{file_path}' not found.")
    except Exception as e:
        logger.exception(f"An error occurred: {e}")


if __name__ == "__main__":
    log_file = "logs.txt" 
    follow_logs(log_file)
