"""
A labeled benchmark for the name matcher: pairs of (name typed by an analyst, name on a list, category).

POSITIVES are the same person written differently (the matcher should flag them). NEGATIVES are different
people (the matcher should not). The pairs are written by hand from the ways names really differ across
passports, bank forms and watch lists: transliteration, word order, titles, initials, fused or split
words, typing mistakes. They are public-figure style names and invented ones; none is a claim about anyone.
`random_negatives` adds ordinary different-people pairs that share a common name token.
"""
import random

POSITIVES = [
    # transliteration of the same name
    ("Mohamed Farouk Khan", "Muhammad Farooq Khan", "spelling"),
    ("Shawkat Ali", "Shaukat Ali", "spelling"),
    ("Raihan Ahmad", "Rehan Ahmed", "spelling"),
    ("Zaffar Iqbal", "Zafar Iqbal", "spelling"),
    ("Naved Akhter", "Naveed Akhtar", "spelling"),
    ("Waqqar Younis", "Waqar Yunis", "spelling"),
    ("Basheer Ahmed", "Bashir Ahmad", "spelling"),
    ("Shaheed Afridi", "Shahid Afridi", "spelling"),
    ("Gulam Mustafa", "Ghulam Mustafa", "spelling"),
    ("Hameed Karzai", "Hamid Karzai", "spelling"),
    ("Mehmood Qureshi", "Mahmood Qureshi", "spelling"),
    ("Shah Mehmood Qureshi", "Shah Mahmood Qureshi", "spelling"),
    ("Khwaja Asif", "Khawaja Asif", "spelling"),
    ("Pervaiz Elahi", "Pervez Elahi", "spelling"),
    ("Yousaf Raza Gillani", "Syed Yusuf Raza Gilani", "spelling"),
    ("Bilal Ahmed", "Belal Ahmad", "spelling"),
    ("Usama Bin Laden", "Osama Bin Ladin", "spelling"),
    ("Abu Bakr Al Baghdadi", "Abu Bakr al-Baghdadi", "spelling"),
    ("Ayman Al Zawahiri", "Ayman al-Zawahri", "spelling"),
    ("Mohammad-Ali Jinnah", "Muhammad Ali Jinnah", "spelling"),
    ("Fatimah Jinnah", "Fatima Jinnah", "spelling"),
    ("Aisha Siddiqui", "Ayesha Siddiqi", "spelling"),
    ("Aleksei Navalny", "Alexey Navalny", "spelling"),
    ("Hasan Nasrallah", "Hassan Nasrallah", "spelling"),
    ("Moammar Gadhafi", "Muammar Gaddafi", "spelling"),
    ("Rizwan Akhtar", "Rizwan Akhter", "spelling"),
    ("Tahir Hussain", "Taher Hussain", "spelling"),
    ("Sajjad Haider", "Sajad Haidar", "spelling"),
    ("Kamran Akmal", "Kamran Akmaal", "spelling"),
    ("Mohammed Tariq Mehmood", "Muhammad Tarik Mahmood", "spelling"),
    # word order
    ("Khan Imran Ahmed", "Imran Ahmed Khan", "order"),
    ("Hafiz Muhammad Saeed", "Muhammad Saeed Hafiz", "order"),
    ("Sharif Nawaz", "Nawaz Sharif", "order"),
    ("Zardari Asif Ali", "Asif Ali Zardari", "order"),
    ("Bhutto Benazir", "Benazir Bhutto", "order"),
    ("Malik Riaz Hussain", "Riaz Hussain Malik", "order"),
    ("Ahmed Raza Shah", "Shah Ahmed Raza", "order"),
    ("Ali Hassan Mirza", "Mirza Ali Hassan", "order"),
    # titles and particles
    ("Dr. Arif Alvi", "Arif Alvi", "title"),
    ("Haji Muhammad Anwar", "Muhammad Anwar", "title"),
    ("Mr Imran Khan", "Imran Khan", "title"),
    ("Prof Dr Tahir Ul Qadri", "Tahir ul Qadri", "title"),
    ("Maulana Fazlur Rehman", "Fazal ur Rehman", "title"),
    ("Engr Shahid Khan", "Shahid Khan", "title"),
    ("Sheikh Rashid Ahmad", "Rashid Ahmad", "title"),
    ("Chaudhry Shujaat Hussain", "Ch Shujaat Hussain", "title"),
    ("Ch. Pervaiz Elahi", "Chaudhry Pervaiz Elahi", "title"),
    ("Mian Muhammad Nawaz Sharif", "Nawaz Sharif", "title"),
    ("Sardar Akhtar Mengal", "Akhtar Mengal", "title"),
    ("Malik Amin Aslam", "Amin Aslam", "title"),
    ("Khawaja Saad Rafique", "Saad Rafique", "title"),
    ("Syed Ali Zafar", "Sayed Ali Zafar", "title"),
    ("Sahibzada Hamid Raza", "Hamid Raza", "title"),
    # initials and missing / extra middle names
    ("M. Imran Khan", "Muhammad Imran Khan", "initial"),
    ("Muhammad A. Khan", "Muhammad Ahmed Khan", "initial"),
    ("Asif Zardari", "Asif Ali Zardari", "missing"),
    ("Nawaz Sharif", "Muhammad Nawaz Sharif", "missing"),
    ("Shahbaz Sharif", "Muhammad Shahbaz Sharif", "missing"),
    ("Bilawal Zardari", "Bilawal Bhutto Zardari", "missing"),
    ("Imran Khan Niazi", "Imran Ahmad Khan Niazi", "missing"),
    ("Ali Raza", "Syed Ali Raza", "missing"),
    ("Hamza Shahbaz", "Hamza Shahbaz Sharif", "missing"),
    ("Saad Hussain Rizvi", "Khadim Hussain Rizvi", "other-person-skip"),
    # fused and split words
    ("Abdulrahman Khan", "Abdul Rahman Khan", "fused"),
    ("Abdur Rehman Malik", "Abdul Rahman Malik", "fused"),
    ("Abdul Rauf Azhar", "Abdur Rauf Azhar", "fused"),
    ("Fazalur Rehman", "Fazal ur Rehman", "fused"),
    ("Sirajul Haq", "Siraj ul Haq", "fused"),
    ("Nurul Hasan", "Noor ul Hassan", "fused"),
    ("Zia ul Haq", "Ziaul Haq", "fused"),
    ("Abdul Hameed", "Abdulhameed", "fused"),
    ("Muhammad Ali", "Muhammadali", "fused"),
    ("Khan Muhammad", "Khanmuhammad", "fused"),
    # typing mistakes
    ("Mohammad Aslm", "Muhammad Aslam", "typo"),
    ("Imran Kahn", "Imran Khan", "typo"),
    ("Bilawal Bhuto", "Bilawal Bhutto", "typo"),
    ("Shahbaz Shariff", "Shahbaz Sharif", "typo"),
    ("Maryam Nawas", "Maryam Nawaz", "typo"),
    ("Fawad Chaudry", "Fawad Chaudhry", "typo"),
    ("Hamid Mir", "Hamid Meer", "typo"),
    ("Talha Mahmod", "Talha Mahmood", "typo"),
    ("Sohail Anwer", "Sohail Anwar", "typo"),
    ("Murad Ali Shah", "Murad Ali Shaah", "typo"),
    ("Muhammd Rafiq", "Muhammad Rafiq", "typo"),
    ("Jahangir Tareen", "Jahangir Tarin", "typo"),
    ("Pervez Musharaf", "Pervez Musharraf", "typo"),
    ("Qamar Javed Bajwa", "Qamar Jawed Bajwa", "typo"),
    # exact
    ("Asif Ali Zardari", "Asif Ali Zardari", "exact"),
    ("Vladimir Putin", "Vladimir Putin", "exact"),
    ("Kim Jong Un", "Kim Jong-un", "exact"),
    ("Hassan Ali Khan", "Hassan Ali Khan", "exact"),
    ("Ayesha Gulalai", "Ayesha Gulalai", "exact"),
    ("Saeed Ghani", "Saeed Ghani", "exact"),
    # a name with an accent or foreign letters
    ("Lukasz Kowalski", "Łukasz Kowalski", "accent"),
    ("Jose Garcia", "José García", "accent"),
    ("Soren Jensen", "Søren Jensen", "accent"),
]
POSITIVES = [p for p in POSITIVES if p[2] != "other-person-skip"]

NEGATIVES = [
    ("Muhammad Ali Khan", "Muhammad Imran Khan", "shares two tokens"),
    ("Ahmed Khan", "Ahmad Raza", "shares given name"),
    ("Imran Khan", "Imran Ali", "shares given name"),
    ("Imran Khan", "Irfan Khan", "similar given name"),
    ("Hassan Ali", "Hussain Ali", "close given names"),
    ("Saeed Ahmad", "Sajid Ahmad", "close given names"),
    ("Asif Ali", "Asif Raza", "shares given name"),
    ("Nawaz Sharif", "Nawaz Khan", "shares given name"),
    ("Shahbaz Sharif", "Shahbaz Gill", "shares given name"),
    ("Shahbaz Sharif", "Waqar Sharif", "shares surname"),
    ("Arif Alvi", "Arif Malik", "shares given name"),
    ("Arif Alvi", "Asif Alvi", "close given names"),
    ("Bilal Khan", "Bilal Hassan", "shares given name"),
    ("Ali Raza", "Ali Hamza", "shares given name"),
    ("Hamza Ali", "Hamid Ali", "close given names"),
    ("Tariq Mehmood", "Tariq Rashid", "shares given name"),
    ("Kamran Khan", "Kamran Akmal", "shares given name"),
    ("Usman Khan", "Usman Ghani", "shares given name"),
    ("Zubair Ahmed", "Zahir Ahmed", "close given names"),
    ("Muhammad Hanif", "Muhammad Hafeez", "close given names"),
    ("Muhammad Yousaf", "Muhammad Younas", "close given names"),
    ("Abdul Rahman Khan", "Abdul Qadir Khan", "shares two tokens"),
    ("Abdul Hameed", "Abdul Majeed", "close second name"),
    ("Fazal ur Rehman", "Fazal ur Haq", "shares two tokens"),
    ("Siraj ul Haq", "Zia ul Haq", "shares particles"),
    ("Hafiz Saeed", "Hafiz Naeem", "shares title"),
    ("Sana Ullah", "Sana Javed", "shares given name"),
    ("Maryam Nawaz", "Maryam Aurangzeb", "shares given name"),
    ("Maryam Nawaz", "Maria Nawaz", "close given names"),
    ("Fatima Jinnah", "Fatima Bhutto", "shares given name"),
    ("Aisha Khan", "Aisha Malik", "shares given name"),
    ("Sohail Khan", "Suhail Malik", "close given names"),
    ("Rashid Minhas", "Rashid Mehmood", "shares given name"),
    ("Farooq Sattar", "Farooq Abdullah", "shares given name"),
    ("Javed Hashmi", "Javed Miandad", "shares given name"),
    ("Mustafa Kamal", "Mustafa Nawaz", "shares given name"),
    ("Naseem Shah", "Naseem Khan", "shares given name"),
    ("Babar Azam", "Babar Awan", "close surnames"),
    ("Shoaib Malik", "Shoaib Akhtar", "shares given name"),
    ("Wasim Akram", "Wasim Bari", "shares given name"),
    ("Mohsin Khan", "Mohsin Naqvi", "shares given name"),
    ("Qasim Ali", "Qasim Rizwan", "shares given name"),
    ("Owais Ahmed", "Uwais Raza", "close given names"),
    ("Sheraz Khan", "Shehzad Khan", "close given names"),
    ("Danish Kaneria", "Danish Ali", "shares given name"),
    ("Ahsan Iqbal", "Ahsan Khan", "shares given name"),
    ("Khurram Dastgir", "Khurram Shahzad", "shares given name"),
    ("Mian Aslam", "Mian Tariq", "shares title"),
    ("John Smith", "Jon Stewart", "different people"),
    ("Maria Garcia", "Mario Garcia", "close given names"),
    ("Ivan Petrov", "Ivan Petrenko", "close surnames"),
    ("Sergei Ivanov", "Sergei Ivanova", "gendered form"),
    ("Li Wei", "Li Wang", "shares given name"),
    ("Wang Fang", "Wang Lei", "shares surname"),
    ("Mohammad Khan", "Mohammad Ahmed", "shares given name"),
    ("Mohammed Hussain", "Mohammed Hassan", "close second name"),
    ("Ali Hussain", "Ali Hassan", "close second name"),
    ("Omar Farooq", "Omar Sharif", "shares given name"),
    ("Ibrahim Khan", "Ibrahim Lodhi", "shares given name"),
    ("Khalid Mahmood", "Khalid Javed", "shares given name"),
    ("Tahir Malik", "Tahir Ashraf", "shares given name"),
    ("Salman Butt", "Salman Khan", "shares given name"),
    ("Raza Rabbani", "Raza Hayat", "shares given name"),
    ("Saleem Mandviwalla", "Saleem Ahmad", "shares given name"),
    ("Murad Saeed", "Murad Ali", "shares given name"),
    ("Hina Rabbani Khar", "Hina Pervaiz Butt", "shares given name"),
    ("Sherry Rehman", "Sherry Khan", "shares given name"),
    ("Ejaz Ahmed", "Ejaz Chaudhry", "shares given name"),
    ("Nadeem Afzal", "Nadeem Baig", "shares given name"),
    ("Junaid Safdar", "Junaid Jamshed", "shares given name"),
    ("Hassan Nawaz", "Hussain Nawaz", "close given names"),
    ("Zain Elahi", "Zain Shah", "shares given name"),
    ("Anwar Majeed", "Anwar Mansoor", "shares given name"),
    ("Mubashir Luqman", "Mubashir Zaidi", "shares given name"),
    ("Hamid Khan", "Hamid Mir", "shares given name"),
    ("Aamir Liaquat", "Aamir Khan", "shares given name"),
    ("Imtiaz Ahmed", "Imtiaz Ali", "shares given name"),
]


def random_negatives(n: int = 120, seed: int = 7) -> list:
    """Ordinary different-people pairs: two or three part names that share one common name part at most."""
    rng = random.Random(seed)
    given = ("Muhammad Ali Ahmed Hassan Hussain Imran Usman Bilal Tariq Zubair Kamran Faisal Naveed Adnan Waqas Hamza "
             "Salman Rizwan Asad Irfan Shahid Nadeem Sajid Farhan Kashif Junaid Haroon Daniyal Talha Danish Saad "
             "Ayesha Fatima Sana Maryam Hira Nida Amna Rabia Sadia Zainab Mehwish Iqra Bushra Saima Uzma").split()
    sur = ("Khan Malik Sheikh Butt Qureshi Chaudhry Raza Shah Mirza Baig Ansari Siddiqui Rana Gill Bhatti Awan Niazi "
           "Lodhi Memon Soomro Bugti Marri Afridi Yousafzai Rehman Javed Iqbal Akhtar Hashmi Naqvi Zaidi Rizvi").split()
    from app.screening import names as nm
    out, seen = [], set()
    while len(out) < n:
        a = (rng.choice(given), rng.choice(given), rng.choice(sur))
        b = list(a)
        which = rng.choice((0, 1, 2))
        keep = rng.choice((0, 2)) if which == 0 else which            # share the surname or one given name only
        b = [rng.choice(given), rng.choice(given), rng.choice(sur)]
        b[keep if keep != 2 else 2] = a[keep if keep != 2 else 2]
        if len(set(a[:2])) < 2 or len(set(b[:2])) < 2:
            continue
        ta, tb = nm.tokens(" ".join(a)), nm.tokens(" ".join(b))
        shared = len(set(ta) & set(tb))
        if shared != 1:
            continue
        # genuinely different people: the remaining tokens must not be spellings of each other
        rest_a, rest_b = [t for t in ta if t not in tb], [t for t in tb if t not in ta]
        if any(nm.jw(x, y) >= 0.80 for x in rest_a for y in rest_b):
            continue
        pair = (" ".join(a), " ".join(b))
        if pair in seen:
            continue
        seen.add(pair)
        out.append((pair[0], pair[1], "random different people"))
    return out
