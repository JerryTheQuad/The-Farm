chicken = 23
goat = 678
pig = 1296
cow = 3848
sheep = 6769
animals = ['chicken', 'goat', 'pig', 'cow', 'sheep']

a = int(input())

if 23 <= a < 678:
    print('{number_animals} {animals}'.format(number_animals=a // 23, animals='chicken' if a < 46 else 'chickens'))
elif 678 <= a < 1296:
    print('{number_animals} {animals}'.format(number_animals=a // 678, animals='goat' if a < 1356 else 'goats'))
elif 1296 <= a < 3848:
    print('{number_animals} {animals}'.format(number_animals=a // 1296, animals='pig' if a < 2592 else 'pigs'))
elif 3848 <= a < 6769:
    print('{number_animals} {animals}'.format(number_animals=a // 3848, animals='cow' if a < 7696 else 'cows'))
elif a >= 6769:
    print('{number_animals} {animals}'.format(number_animals=a // 6769, animals='sheep'))
elif a < 23:
    print('None')
